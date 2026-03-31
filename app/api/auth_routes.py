"""User authentication routes for the Phoenix platform.

Implements a PostgreSQL-backed user store with register, login, and me endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import contextmanager
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.api.models import Role
from app.data.postgres import connect_with_retry, get_control_plane_dsn

try:  # pragma: no cover - optional dependency in local/dev
    import bcrypt  # type: ignore
except Exception:  # pragma: no cover - optional dependency in local/dev
    bcrypt = None


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    """User signup request."""

    name: str
    email: EmailStr
    password: str


class SignupResponse(BaseModel):
    """Signup response."""

    id: str
    email: str
    name: str
    message: str


class LoginRequest(BaseModel):
    """User login request."""

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Login response."""

    token: str
    user: dict


_TOKEN_TTL_SECONDS = 60 * 60


@contextmanager
def _conn():
    dsn = get_control_plane_dsn()
    conn = connect_with_retry(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _token_secret() -> str:
    from app.config.settings import get_settings
    settings = get_settings()
    configured = str(
        getattr(settings, "demo_auth_token_secret", None)
        or os.getenv("DEMO_AUTH_TOKEN_SECRET", "")
    ).strip()
    if configured:
        return configured
    return "phoenix-demo-auth-secret"


def _make_token(*, user_id: str, email: str, role: Role) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "email": email,
        "role": role.value,
        "iat": now,
        "exp": now + _TOKEN_TTL_SECONDS,
    }
    header_part = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_part = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(
        _token_secret().encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"{header_part}.{payload_part}.{_b64url_encode(signature)}"


def _parse_token(token: str) -> dict | None:
    try:
        header_part, payload_part, signature_part = token.split(".", 2)
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        expected_signature = hmac.new(
            _token_secret().encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(
            expected_signature,
            _b64url_decode(signature_part),
        ):
            return None
        payload_obj = json.loads(_b64url_decode(payload_part).decode("utf-8"))
        exp = int(payload_obj.get("exp", 0))
        if exp <= int(time.time()):
            return None
        return payload_obj
    except Exception:
        return None


def _hash_password(password: str) -> str:
    value = str(password or "")
    if bcrypt is not None:
        digest = bcrypt.hashpw(value.encode("utf-8"), bcrypt.gensalt())
        return f"bcrypt${digest.decode('utf-8')}"
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt.encode("utf-8"),
        200_000,
    )
    return f"pbkdf2${salt}${_b64url_encode(key)}"


def _verify_password(password: str, password_hash: str) -> bool:
    value = str(password or "")
    if password_hash.startswith("bcrypt$") and bcrypt is not None:
        digest = password_hash.split("$", 1)[1]
        return bool(
            bcrypt.checkpw(value.encode("utf-8"), digest.encode("utf-8"))
        )
    if password_hash.startswith("pbkdf2$"):
        _, salt, encoded = password_hash.split("$", 2)
        key = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode("utf-8"),
            salt.encode("utf-8"),
            200_000,
        )
        return hmac.compare_digest(encoded, _b64url_encode(key))
    return False


def _get_user_by_email(email: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, email, name, password_hash, role FROM users WHERE email = %(email)s",
                {"email": email},
            )
            return cur.fetchone()


def _get_user_by_id(user_id: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, email, name, password_hash, role FROM users WHERE id = %(id)s",
                {"id": user_id},
            )
            return cur.fetchone()


def _create_user(*, user_id: str, email: str, name: str, password_hash: str, role: Role) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, name, password_hash, role)
                VALUES (%(id)s, %(email)s, %(name)s, %(password_hash)s, %(role)s)
                """,
                {
                    "id": user_id,
                    "email": email,
                    "name": name,
                    "password_hash": password_hash,
                    "role": role.value,
                },
            )


def _update_user_role(email: str, role: Role) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET role = %(role)s, updated_at = NOW()
                WHERE email = %(email)s
                """,
                {"role": role.value, "email": email},
            )
            return cur.rowcount > 0


def _next_user_id() -> str:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            row = cur.fetchone()
            count = row[0] if row else 0
    return f"user_{count + 1}"


@router.post("/register", response_model=SignupResponse)
async def register(request: SignupRequest) -> SignupResponse:
    """Register a new user."""
    existing = _get_user_by_email(request.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already exists with this email",
        )

    user_id = _next_user_id()
    _create_user(
        user_id=user_id,
        email=request.email,
        name=request.name,
        password_hash=_hash_password(request.password),
        role=Role.VIEWER,
    )

    logger.info("User registered: %s", request.email)

    return SignupResponse(
        id=user_id,
        email=request.email,
        name=request.name,
        message="User created successfully",
    )


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest) -> LoginResponse:
    """Login user."""
    user = _get_user_by_email(request.email)
    if not user or not _verify_password(request.password, str(user.get("password_hash", ""))):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    role = Role(user["role"])
    token = _make_token(user_id=str(user["id"]), email=str(user["email"]), role=role)
    logger.info("User logged in: %s", request.email)

    return LoginResponse(
        token=token,
        user={"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]},
    )


@router.get("/me")
async def get_current_user(token: Optional[str] = None) -> dict:
    """Get current user info."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    payload = _parse_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    email = str(payload.get("email") or "").strip()
    user_id = str(payload.get("sub") or "").strip()
    user = _get_user_by_email(email)
    if user is None or str(user.get("id") or "") != user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
    }


class PromoteRequest(BaseModel):
    email: EmailStr
    role: Role


@router.post("/promote")
async def promote_user(request: PromoteRequest):
    """Promote a user to a new role."""
    updated = _update_user_role(request.email, request.role)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    logger.info("User %s promoted to %s", request.email, request.role)
    return {"message": f"User {request.email} promoted to {request.role}"}


@router.get("/callback")
async def oauth_callback():
    """SmartAPI portal compliance endpoint. Phoenix uses server-side TOTP login; this redirect is never triggered."""
    return {"status": "ok"}
