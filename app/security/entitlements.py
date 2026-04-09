from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

try:  # pragma: no cover - optional in lightweight test environments
    from psycopg.rows import tuple_row  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    tuple_row = None

from app.api.models import Role
from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)

_USER_TENANT_TABLE = "user_tenant_entitlements"
_USER_BROKER_ACCOUNT_TABLE = "user_broker_account_entitlements"


@dataclass(frozen=True)
class UserEntitlements:
    tenant_ids: tuple[str, ...] = ()
    broker_account_ids: tuple[str, ...] = ()
    all_tenants: bool = False
    source: str = "none"


def _normalize_identifiers(values: Iterable[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return tuple(normalized)


def _role_token(role: Any) -> str:
    if isinstance(role, Role):
        return role.value
    return str(role or "").strip().lower()


def _all_tenants_role(role: Any) -> bool:
    return _role_token(role) == Role.ADMIN.value


def _connect():
    dsn = get_control_plane_dsn()
    return connect_with_retry(dsn, autocommit=True)


def _table_exists(conn, table_name: str) -> bool:
    if tuple_row is None:
        return False
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
        row = cur.fetchone()
    return bool(row and row[0])


def _load_postgres_entitlements(user_id: str) -> UserEntitlements:
    if not user_id or tuple_row is None:
        return UserEntitlements(source="postgres")

    try:
        with _connect() as conn:
            tenant_ids: tuple[str, ...] = ()
            broker_account_ids: tuple[str, ...] = ()

            if _table_exists(conn, _USER_TENANT_TABLE):
                with conn.cursor(row_factory=tuple_row) as cur:
                    cur.execute(
                        f"SELECT tenant_id FROM {_USER_TENANT_TABLE} WHERE user_id = %s ORDER BY tenant_id",
                        (user_id,),
                    )
                    tenant_ids = _normalize_identifiers(row[0] for row in cur.fetchall())

            if _table_exists(conn, _USER_BROKER_ACCOUNT_TABLE):
                with conn.cursor(row_factory=tuple_row) as cur:
                    cur.execute(
                        f"SELECT broker_account_id FROM {_USER_BROKER_ACCOUNT_TABLE} WHERE user_id = %s ORDER BY broker_account_id",
                        (user_id,),
                    )
                    broker_account_ids = _normalize_identifiers(
                        row[0] for row in cur.fetchall()
                    )

            return UserEntitlements(
                tenant_ids=tenant_ids,
                broker_account_ids=broker_account_ids,
                all_tenants=False,
                source="postgres",
            )
    except Exception:
        logger.exception("Failed to load user entitlements from Postgres for user_id=%s", user_id)
        return UserEntitlements(source="postgres")


def _env_payload() -> dict[str, Any]:
    raw = str(
        os.getenv("PHOENIX_USER_ENTITLEMENTS_JSON")
        or os.getenv("USER_TENANT_ENTITLEMENTS_JSON")
        or ""
    ).strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        logger.exception("Invalid PHOENIX_USER_ENTITLEMENTS_JSON payload; ignoring")
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_env_entitlements(user_id: str, email: str) -> UserEntitlements:
    payload = _env_payload()
    if not payload:
        return UserEntitlements(source="env")

    users_payload = payload.get("users") if isinstance(payload.get("users"), dict) else payload
    candidate = None
    for key in (user_id, email):
        if key and isinstance(users_payload, dict) and key in users_payload:
            candidate = users_payload.get(key)
            break
    if not isinstance(candidate, dict):
        return UserEntitlements(source="env")

    raw_tenants = candidate.get("tenants") or candidate.get("tenant_ids") or []
    raw_accounts = candidate.get("broker_accounts") or candidate.get("broker_account_ids") or []
    all_tenants = bool(candidate.get("all_tenants")) or raw_tenants == "*"
    return UserEntitlements(
        tenant_ids=_normalize_identifiers(raw_tenants if isinstance(raw_tenants, list) else []),
        broker_account_ids=_normalize_identifiers(
            raw_accounts if isinstance(raw_accounts, list) else []
        ),
        all_tenants=all_tenants,
        source="env",
    )


def resolve_user_entitlements(*, user_id: str, email: str, role: Any) -> UserEntitlements:
    if _all_tenants_role(role):
        return UserEntitlements(all_tenants=True, source="role")

    pg_entitlements = _load_postgres_entitlements(user_id)
    if pg_entitlements.tenant_ids or pg_entitlements.broker_account_ids:
        return pg_entitlements

    env_entitlements = _load_env_entitlements(user_id, email)
    if env_entitlements.tenant_ids or env_entitlements.broker_account_ids or env_entitlements.all_tenants:
        return env_entitlements

    return UserEntitlements(source="none")


__all__ = ["UserEntitlements", "resolve_user_entitlements"]
