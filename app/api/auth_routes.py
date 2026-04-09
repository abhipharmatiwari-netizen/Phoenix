"""User authentication routes for the Phoenix platform.

Implements a PostgreSQL-backed user store with register, login, and authenticated
identity endpoints.
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
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

try:  # pragma: no cover - optional in lightweight test environments
    from psycopg.rows import dict_row  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    dict_row = None
from pydantic import BaseModel, EmailStr

from app.api.models import Role
from app.core.audit_log import emit_audit_event
from app.data.postgres import connect_with_retry, get_control_plane_dsn
from app.security.entitlements import resolve_user_entitlements

try:  # pragma: no cover - optional dependency in local/dev
    import bcrypt  # type: ignore
except Exception:  # pragma: no cover - optional dependency in local/dev
    bcrypt = None


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_TOKEN_TTL_SECONDS = 60 * 60  # 1 hour access token
_REFRESH_INCLUDE = True  # set False to disable refresh token issuance


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
    refresh_token: Optional[str] = None
    expires_in: int = _TOKEN_TTL_SECONDS
    user: dict[str, Any]


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


def _app_env() -> str:
    return str(os.getenv("APP_ENV") or os.getenv("ENV") or "local").strip().lower() or "local"


def _token_secret() -> str:
    from app.config.settings import get_settings

    settings = get_settings()
    configured = str(
        getattr(settings, "demo_auth_token_secret", None)
        or os.getenv("DEMO_AUTH_TOKEN_SECRET", "")
    ).strip()
    if configured:
        return configured
    raise RuntimeError(
        "DEMO_AUTH_TOKEN_SECRET must be configured explicitly; implicit auth-secret fallback is disabled"
    )


def _normalize_role(value: object) -> Role:
    token = value.value if isinstance(value, Role) else str(value or "").strip().lower()
    try:
        return Role(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user role",
        ) from exc


def _make_token(*, user_id: str, email: str, role: Role, jti: Optional[str] = None) -> str:
    now = int(time.time())
    token_jti = jti or secrets.token_hex(16)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "email": email,
        "role": role.value,
        "jti": token_jti,
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
        # PHX-SEC-005: check revocation list
        jti = str(payload_obj.get("jti") or "").strip()
        if jti:
            try:
                from app.core.session_store import is_revoked
                if is_revoked(jti):
                    return None
            except Exception:
                pass
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
    if dict_row is None:
        raise RuntimeError("psycopg is required for auth database operations")
    with _conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, email, name, password_hash, role FROM users WHERE email = %(email)s",
                {"email": email},
            )
            return cur.fetchone()


def _get_user_by_id(user_id: str) -> dict | None:
    if dict_row is None:
        raise RuntimeError("psycopg is required for auth database operations")
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


def _extract_bearer_token(authorization: Optional[str]) -> str:
    value = str(authorization or "").strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def _serialize_user(user: dict[str, Any]) -> dict[str, Any]:
    role = _normalize_role(user.get("role"))
    entitlements = resolve_user_entitlements(
        user_id=str(user.get("id") or "").strip(),
        email=str(user.get("email") or "").strip(),
        role=role,
    )
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": role.value,
        "tenant_ids": list(entitlements.tenant_ids),
        "broker_account_ids": list(entitlements.broker_account_ids),
        "can_access_all_tenants": entitlements.all_tenants,
        "entitlements_source": entitlements.source,
    }


def authenticate_bearer_token(authorization: Optional[str]) -> dict[str, Any]:
    token = _extract_bearer_token(authorization)
    payload = _parse_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email = str(payload.get("email") or "").strip()
    user_id = str(payload.get("sub") or "").strip()
    if not email or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = _get_user_by_id(user_id) or _get_user_by_email(email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if str(user.get("id") or "") != user_id or str(user.get("email") or "").strip() != email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {
        "id": str(user["id"]),
        "email": str(user["email"]),
        "name": str(user.get("name") or ""),
        "role": _normalize_role(user.get("role")),
    }


async def require_authenticated_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    return authenticate_bearer_token(authorization)


async def require_admin_user(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    if current_user["role"] != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return current_user


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
    """Login user. Returns a short-lived access token and a long-lived refresh token."""
    user = _get_user_by_email(request.email)
    if not user or not _verify_password(request.password, str(user.get("password_hash", ""))):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    role = _normalize_role(user["role"])
    token = _make_token(user_id=str(user["id"]), email=str(user["email"]), role=role)
    logger.info("User logged in: %s", request.email)

    refresh_token: Optional[str] = None
    if _REFRESH_INCLUDE:
        try:
            from app.core.session_store import issue_refresh_token
            refresh_token, _ = issue_refresh_token(
                user_id=str(user["id"]),
                email=str(user["email"]),
                role=role.value,
            )
        except Exception:
            logger.debug("Refresh token issuance skipped", exc_info=True)

    return LoginResponse(
        token=token,
        refresh_token=refresh_token,
        expires_in=_TOKEN_TTL_SECONDS,
        user={
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": role.value,
        },
    )


@router.post("/refresh")
async def refresh_access_token(
    body: dict,
) -> dict:
    """Exchange a refresh token for a new access token (PHX-SEC-005)."""
    refresh_token = str(body.get("refresh_token") or "").strip()
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="refresh_token is required",
        )
    try:
        from app.core.session_store import consume_refresh_token
        data = consume_refresh_token(refresh_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session store error",
        ) from exc

    if data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user = _get_user_by_id(str(data["user_id"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    role = _normalize_role(user["role"])
    new_token = _make_token(user_id=str(user["id"]), email=str(user["email"]), role=role)

    from app.core.session_store import issue_refresh_token
    new_refresh, _ = issue_refresh_token(
        user_id=str(user["id"]),
        email=str(user["email"]),
        role=role.value,
    )
    logger.info("Token refreshed for user: %s", user["email"])
    return {
        "token": new_token,
        "refresh_token": new_refresh,
        "expires_in": _TOKEN_TTL_SECONDS,
    }


@router.post("/logout")
async def logout(
    current_user: dict[str, Any] = Depends(require_authenticated_user),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict:
    """Revoke the current session token and all refresh tokens (PHX-SEC-005)."""
    token = _extract_bearer_token(authorization)
    payload = _parse_token.__wrapped__(token) if hasattr(_parse_token, "__wrapped__") else None
    # Re-parse without revocation check to get the jti
    try:
        header_part, payload_part, _ = token.split(".", 2)
        import json as _json
        raw = _json.loads(_b64url_decode(payload_part).decode("utf-8"))
        jti = str(raw.get("jti") or "").strip()
        exp = int(raw.get("exp", 0))
        if jti:
            from app.core.session_store import revoke_token
            revoke_token(jti, exp)
    except Exception:
        logger.debug("Could not extract jti for revocation", exc_info=True)

    try:
        from app.core.session_store import revoke_all_for_user
        revoke_all_for_user(str(current_user["id"]))
    except Exception:
        logger.debug("Refresh token revocation failed", exc_info=True)

    emit_audit_event(
        actor=str(current_user["email"]),
        action="logout",
        resource_type="session",
        resource_id=str(current_user["id"]),
        metadata={"user_id": str(current_user["id"])},
    )
    logger.info("User logged out: %s", current_user["email"])
    return {"message": "Logged out successfully"}


@router.get("/me")
async def get_current_user(
    token: Optional[str] = Query(default=None),
    current_user: dict[str, Any] = Depends(require_authenticated_user),
) -> dict[str, Any]:
    """Return the authenticated user and server-side entitlements."""
    if token is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token query parameters are no longer accepted; use Authorization: Bearer.",
        )
    return _serialize_user(current_user)


class PromoteRequest(BaseModel):
    email: EmailStr
    role: Role


@router.post("/promote")
async def promote_user(
    request: PromoteRequest,
    actor: dict[str, Any] = Depends(require_admin_user),
):
    """Promote a user to a new role. Admin-only and always audited."""
    before = _get_user_by_email(request.email)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    updated = _update_user_role(request.email, request.role)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    logger.warning(
        "User role updated actor=%s target=%s new_role=%s app_env=%s",
        actor["email"],
        request.email,
        request.role.value,
        _app_env(),
    )
    emit_audit_event(
        actor=str(actor["email"]),
        action="promote_user",
        resource_type="user",
        resource_id=str(before["id"]),
        before={"email": before["email"], "role": str(before.get("role") or "")},
        after={"email": request.email, "role": request.role.value},
        metadata={
            "actor_user_id": str(actor["id"]),
            "target_email": str(request.email),
        },
    )
    return {
        "message": f"User {request.email} promoted to {request.role.value}",
        "changed_by": actor["email"],
    }


@router.get("/callback")
async def oauth_callback():
    """SmartAPI portal compliance endpoint. Phoenix uses server-side TOTP login; this redirect is never triggered."""
    return {"status": "ok"}


__all__ = [
    "authenticate_bearer_token",
    "require_admin_user",
    "require_authenticated_user",
    "router",
    "_get_user_by_email",
    "_get_user_by_id",
    "_parse_token",
    "_token_secret",
    "_make_token",
]
