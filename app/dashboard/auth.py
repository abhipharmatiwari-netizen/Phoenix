"""Auth and context helpers for dashboard APIs.

Provides:
- AdminContext resolved from authenticated bearer identity, static admin key, or HMAC.
- TenantContext resolved from entitlement-backed tenant scope.
- Role-based access control: READONLY, OPERATOR, ADMIN roles.
- Non-local auth enforcement: auth is mandatory when APP_ENV is not local/dev.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Request, status

from app.api.auth_routes import authenticate_bearer_token
from app.api.models import Role
from app.config.settings import get_settings
from app.core.identifiers import TenantId
from app.security.entitlements import resolve_user_entitlements
from app.tenants.firestore_client import get_tenant

logger = logging.getLogger(__name__)
_DASHBOARD_WS_TICKET_VERSION = 1
_DASHBOARD_WS_TICKET_TTL_SECONDS = 30


class AdminRole(str, Enum):
    READONLY = "readonly"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class DashboardWebsocketTicket:
    token: str
    issued_at: int
    expires_at: int
    path: str
    mode: str
    nonce: str
    caller: str
    role: AdminRole


_ROLE_HIERARCHY = {
    AdminRole.READONLY: 0,
    AdminRole.OPERATOR: 1,
    AdminRole.ADMIN: 2,
}


def _role_has_permission(role: AdminRole, required: AdminRole) -> bool:
    return _ROLE_HIERARCHY.get(role, 0) >= _ROLE_HIERARCHY.get(required, 0)


def _resolve_role_from_key(admin_key: Optional[str]) -> AdminRole:
    """Resolve role from admin key suffix convention.

    Key format: <secret> or <secret>:readonly or <secret>:operator
    Default role is ADMIN for backward compatibility.
    """
    if not admin_key:
        return AdminRole.ADMIN
    parts = admin_key.rsplit(":", 1)
    if len(parts) == 2 and parts[1] in {r.value for r in AdminRole}:
        return AdminRole(parts[1])
    return AdminRole.ADMIN


class AdminContext:
    def __init__(
        self,
        caller: str = "admin",
        role: AdminRole = AdminRole.ADMIN,
        *,
        user_id: str | None = None,
        email: str | None = None,
        auth_source: str = "admin_key",
        tenant_ids: tuple[str, ...] = (),
        broker_account_ids: tuple[str, ...] = (),
        all_tenants: bool = False,
    ) -> None:
        self.caller = caller
        self.role = role
        self.user_id = user_id
        self.email = email
        self.auth_source = auth_source
        self.tenant_ids = tenant_ids
        self.broker_account_ids = broker_account_ids
        self.all_tenants = all_tenants

    def require_role(self, required: AdminRole) -> None:
        """Raise 403 if current role is insufficient."""
        if not _role_has_permission(self.role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required.value} role, current role is {self.role.value}.",
            )

    def can_access_tenant(self, tenant_id: str) -> bool:
        if self.all_tenants:
            return True
        token = str(tenant_id or "").strip()
        return bool(token and token in self.tenant_ids)

    def can_access_broker_account(self, broker_account_id: str) -> bool:
        if self.all_tenants:
            return True
        if not self.broker_account_ids:
            return True
        token = str(broker_account_id or "").strip()
        return bool(token and token in self.broker_account_ids)


class TenantContext:
    def __init__(
        self,
        tenant_id: TenantId,
        *,
        broker_account_ids: tuple[str, ...] = (),
        source: str = "header",
    ) -> None:
        self.tenant_id = tenant_id
        self.broker_account_ids = broker_account_ids
        self.source = source



def safe_compare_token(expected: Optional[str], actual: Optional[str]) -> bool:
    expected_text = str(expected or "").strip()
    actual_text = str(actual or "").strip()
    if not expected_text or not actual_text:
        return False
    # encode to bytes: hmac.compare_digest rejects str with non-ASCII code points
    # (e.g. UTF-8 BOM injected by PowerShell 5.1 WriteAllText with the default
    # UTF8 encoder, which includes the BOM and is then read by the container shell).
    return hmac.compare_digest(
        expected_text.encode("utf-8"), actual_text.encode("utf-8")
    )



def _dashboard_ws_ticket_secret(settings: object | None = None) -> str:
    cfg = settings or get_settings()
    secret = str(
        getattr(cfg, "dashboard_hmac_secret", None)
        or getattr(cfg, "admin_api_key", None)
        or ""
    ).strip()
    if not secret:
        raise RuntimeError("Dashboard websocket ticket signing secret is not configured")
    return secret



def _urlsafe_b64encode_bytes(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")



def _urlsafe_b64decode_text(payload: str) -> bytes:
    token = str(payload or "").strip()
    if not token:
        raise ValueError("empty token segment")
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(f"{token}{padding}".encode("ascii"))
    except Exception as exc:
        raise ValueError("invalid base64 token segment") from exc



def _dashboard_ws_mode_token(mode: object) -> str:
    token = str(mode or "delta").strip().lower()
    if token not in {"delta", "full"}:
        raise ValueError(f"Unsupported dashboard websocket mode: {token!r}")
    return token



def issue_dashboard_ws_ticket(
    *,
    admin_ctx: AdminContext,
    path: str,
    mode: str,
    settings: object | None = None,
    now_ts: int | None = None,
    ttl_seconds: int | None = None,
    nonce: str | None = None,
) -> DashboardWebsocketTicket:
    secret = _dashboard_ws_ticket_secret(settings)
    issued_at = int(now_ts if now_ts is not None else time.time())
    ttl = max(1, int(ttl_seconds or _DASHBOARD_WS_TICKET_TTL_SECONDS))
    expires_at = issued_at + ttl
    resolved_path = str(path or "").strip() or "/ws/dashboard"
    resolved_mode = _dashboard_ws_mode_token(mode)
    resolved_nonce = str(nonce or uuid4().hex).strip() or uuid4().hex
    payload = {
        "v": _DASHBOARD_WS_TICKET_VERSION,
        "iat": issued_at,
        "exp": expires_at,
        "path": resolved_path,
        "mode": resolved_mode,
        "nonce": resolved_nonce,
        "caller": admin_ctx.caller,
        "role": admin_ctx.role.value,
        "tenant_ids": list(admin_ctx.tenant_ids),
        "broker_account_ids": list(admin_ctx.broker_account_ids),
        "all_tenants": bool(admin_ctx.all_tenants),
    }
    payload_segment = _urlsafe_b64encode_bytes(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature_segment = _urlsafe_b64encode_bytes(
        hmac.new(
            secret.encode("utf-8"),
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return DashboardWebsocketTicket(
        token=f"{payload_segment}.{signature_segment}",
        issued_at=issued_at,
        expires_at=expires_at,
        path=resolved_path,
        mode=resolved_mode,
        nonce=resolved_nonce,
        caller=admin_ctx.caller,
        role=admin_ctx.role,
    )



def verify_dashboard_ws_ticket(
    ticket: str | None,
    *,
    path: str,
    mode: str,
    settings: object | None = None,
    now_ts: int | None = None,
) -> AdminContext:
    token = str(ticket or "").strip()
    if not token:
        raise ValueError("missing dashboard websocket ticket")
    parts = token.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("invalid dashboard websocket ticket format")

    payload_segment, signature_segment = parts
    expected_signature = _urlsafe_b64encode_bytes(
        hmac.new(
            _dashboard_ws_ticket_secret(settings).encode("utf-8"),
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(expected_signature, signature_segment):
        raise ValueError("invalid dashboard websocket ticket signature")

    try:
        payload = json.loads(_urlsafe_b64decode_text(payload_segment).decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid dashboard websocket ticket payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid dashboard websocket ticket payload")
    if int(payload.get("v", 0) or 0) != _DASHBOARD_WS_TICKET_VERSION:
        raise ValueError("unsupported dashboard websocket ticket version")

    current_ts = int(now_ts if now_ts is not None else time.time())
    expires_at = int(payload.get("exp", 0) or 0)
    if expires_at <= current_ts:
        raise ValueError("dashboard websocket ticket expired")

    expected_path = str(path or "").strip() or "/ws/dashboard"
    actual_path = str(payload.get("path") or "").strip()
    if actual_path != expected_path:
        raise ValueError("dashboard websocket ticket path mismatch")

    expected_mode = _dashboard_ws_mode_token(mode)
    actual_mode = _dashboard_ws_mode_token(payload.get("mode"))
    if actual_mode != expected_mode:
        raise ValueError("dashboard websocket ticket mode mismatch")

    role_value = str(payload.get("role") or AdminRole.ADMIN.value).strip().lower()
    role = AdminRole(role_value) if role_value in {r.value for r in AdminRole} else AdminRole.ADMIN
    caller = str(payload.get("caller") or "dashboard_ticket").strip() or "dashboard_ticket"
    tenant_ids = tuple(
        str(value).strip()
        for value in (payload.get("tenant_ids") or [])
        if str(value or "").strip()
    )
    broker_account_ids = tuple(
        str(value).strip()
        for value in (payload.get("broker_account_ids") or [])
        if str(value or "").strip()
    )
    return AdminContext(
        caller=caller,
        role=role,
        auth_source="ws_ticket",
        tenant_ids=tenant_ids,
        broker_account_ids=broker_account_ids,
        all_tenants=bool(payload.get("all_tenants", False)),
    )



def _normalized_signature(value: str) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("sha256="):
        token = token.split("=", 1)[1]
    return token.lower()



def _header_text(value: object) -> Optional[str]:
    """Normalize direct-call test values and FastAPI Header defaults."""
    if value is None:
        return None
    if isinstance(value, str):
        token = value.strip()
        return token or None
    return None



def _is_valid_admin_hmac(
    *,
    request: Request | None,
    timestamp_header: Optional[str],
    signature_header: Optional[str],
) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "dashboard_hmac_auth_enabled", False)):
        return False
    if not timestamp_header or not signature_header:
        return False
    secret = str(
        getattr(settings, "dashboard_hmac_secret", None)
        or settings.admin_api_key
        or ""
    ).strip()
    if not secret:
        return False
    try:
        timestamp_value = int(str(timestamp_header).strip())
    except Exception:
        return False
    max_skew = int(getattr(settings, "dashboard_hmac_max_skew_seconds", 300) or 300)
    if abs(int(time.time()) - timestamp_value) > max(max_skew, 1):
        return False
    method = (
        request.method.upper()
        if request is not None and getattr(request, "method", None)
        else "GET"
    )
    path = ""
    if request is not None:
        scope_path = getattr(request, "scope", {}).get("path")
        if isinstance(scope_path, str) and scope_path:
            path = scope_path
        elif getattr(request, "url", None) is not None:
            path = request.url.path
    payload = f"{timestamp_value}:{method}:{path}".encode("utf-8")
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(
        expected_signature,
        _normalized_signature(signature_header),
    )



def _is_auth_required() -> bool:
    """Auth is mandatory unless APP_ENV is explicitly local or dev."""
    app_env = os.getenv("APP_ENV", "").strip().lower()
    dashboard_auth_disabled = os.getenv("DASHBOARD_AUTH_DISABLED", "").strip().lower()
    if app_env in ("local", "dev", "test"):
        return dashboard_auth_disabled not in ("1", "true", "yes", "on")
    return True



def _admin_role_from_user_role(role: object) -> AdminRole:
    token = role.value if isinstance(role, Role) else str(role or "").strip().lower()
    if token == Role.ADMIN.value:
        return AdminRole.ADMIN
    if token == Role.OPERATOR.value:
        return AdminRole.OPERATOR
    return AdminRole.READONLY



def _user_admin_context(authorization: Optional[str]) -> AdminContext:
    user = authenticate_bearer_token(authorization)
    entitlements = resolve_user_entitlements(
        user_id=str(user.get("id") or ""),
        email=str(user.get("email") or ""),
        role=user.get("role"),
    )
    email = str(user.get("email") or "").strip()
    return AdminContext(
        caller=email or str(user.get("id") or "user"),
        role=_admin_role_from_user_role(user.get("role")),
        user_id=str(user.get("id") or "").strip() or None,
        email=email or None,
        auth_source="bearer",
        tenant_ids=tuple(entitlements.tenant_ids),
        broker_account_ids=tuple(entitlements.broker_account_ids),
        all_tenants=bool(entitlements.all_tenants),
    )


async def get_admin_context(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    x_admin_timestamp: Optional[str] = Header(default=None, alias="X-Admin-Timestamp"),
    x_admin_signature: Optional[str] = Header(default=None, alias="X-Admin-Signature"),
    x_admin_role: Optional[str] = Header(default=None, alias="X-Admin-Role"),
) -> AdminContext:
    auth_required = _is_auth_required()
    authz_header = _header_text(authorization)
    admin_key = _header_text(x_admin_key)
    admin_timestamp = _header_text(x_admin_timestamp)
    admin_signature = _header_text(x_admin_signature)
    admin_role = _header_text(x_admin_role)

    if authz_header:
        ctx = _user_admin_context(authz_header)
        request.state.admin_context = ctx
        return ctx

    if admin_timestamp or admin_signature:
        if _is_valid_admin_hmac(
            request=request,
            timestamp_header=admin_timestamp,
            signature_header=admin_signature,
        ):
            role = (
                AdminRole(admin_role)
                if admin_role and admin_role in {r.value for r in AdminRole}
                else AdminRole.ADMIN
            )
            ctx = AdminContext(caller="admin_hmac", role=role, auth_source="admin_hmac", all_tenants=True)
            request.state.admin_context = ctx
            return ctx
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin HMAC headers.",
        )

    settings = get_settings()

    if not auth_required:
        if not settings.admin_api_key or not admin_key:
            ctx = AdminContext(caller="local_bypass", role=AdminRole.ADMIN, auth_source="local_bypass", all_tenants=True)
            request.state.admin_context = ctx
            return ctx

    if not settings.admin_api_key:
        if auth_required:
            logger.error("Admin API key not configured but auth is required (non-local env)")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Admin API key not configured.",
            )
        ctx = AdminContext(caller="unconfigured", role=AdminRole.ADMIN, auth_source="unconfigured", all_tenants=True)
        request.state.admin_context = ctx
        return ctx

    if not admin_key:
        if auth_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization or X-Admin-Key header.",
            )
        ctx = AdminContext(caller="local_bypass", role=AdminRole.ADMIN, auth_source="local_bypass", all_tenants=True)
        request.state.admin_context = ctx
        return ctx

    compare_key = admin_key.rsplit(":", 1)[0] if ":" in admin_key else admin_key
    if not safe_compare_token(settings.admin_api_key, compare_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin API key.",
        )

    role = _resolve_role_from_key(admin_key)
    ctx = AdminContext(caller="admin", role=role, auth_source="admin_key", all_tenants=True)
    request.state.admin_context = ctx
    return ctx


async def get_tenant_context(
    request: Request,
    admin_ctx: AdminContext = Depends(get_admin_context),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> TenantContext:
    requested_tenant_id = _header_text(x_tenant_id)

    if admin_ctx.auth_source == "bearer":
        if admin_ctx.all_tenants:
            if not requested_tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tenant selection is required.",
                )
            resolved_tenant_id = requested_tenant_id
        else:
            if not admin_ctx.tenant_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No tenant entitlements configured for this user.",
                )
            if requested_tenant_id:
                resolved_tenant_id = requested_tenant_id
            elif len(admin_ctx.tenant_ids) == 1:
                resolved_tenant_id = admin_ctx.tenant_ids[0]
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tenant selection is required.",
                )
            if resolved_tenant_id not in admin_ctx.tenant_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Tenant access denied.",
                )
    else:
        if not requested_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Tenant-Id header.",
            )
        resolved_tenant_id = requested_tenant_id

    tenant = get_tenant(resolved_tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found.",
        )

    ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        broker_account_ids=tuple(admin_ctx.broker_account_ids) if admin_ctx.auth_source == "bearer" and not admin_ctx.all_tenants else (),
        source=admin_ctx.auth_source,
    )
    request.state.tenant_context = ctx
    return ctx


__all__ = [
    "AdminContext",
    "AdminRole",
    "DashboardWebsocketTicket",
    "TenantContext",
    "get_admin_context",
    "get_tenant_context",
    "issue_dashboard_ws_ticket",
    "safe_compare_token",
    "verify_dashboard_ws_ticket",
]
