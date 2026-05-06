from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import json

try:  # pragma: no cover - optional in lightweight test environments
    import psycopg  # type: ignore
    from psycopg.rows import dict_row  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    psycopg = None  # type: ignore[assignment]
    dict_row = None

from app.data.postgres import get_control_plane_dsn, connect_with_retry
from app.core.identifiers import BrokerAccountId, TenantId
from app.tenants.models import (
    BrokerAccountModel,
    StrategyConfigModel,
    SubscriptionModel,
    TenantModel,
)


def _require_psycopg() -> None:
    if psycopg is None or dict_row is None:
        raise RuntimeError("psycopg is required for Postgres control-plane operations")


@contextmanager
def _conn():
    _require_psycopg()
    dsn = get_control_plane_dsn()
    conn = connect_with_retry(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _fetch_one(sql: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def _fetch_all(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def _normalize_broker_account_row(row: Dict[str, Any]) -> Dict[str, Any]:
    if row.get("default_strategies") is None:
        row["default_strategies"] = []
    if row.get("meta") is None:
        row["meta"] = {}
    return row


def _normalize_strategy_config_row(row: Dict[str, Any]) -> Dict[str, Any]:
    if row.get("params") is None:
        row["params"] = {}
    return row


def upsert_tenant(model: TenantModel) -> TenantModel:
    data = model.model_dump()
    sql = """
        INSERT INTO tenants (
            tenant_id, name, email, phone, status, notes, created_at, updated_at
        ) VALUES (
            %(tenant_id)s, %(name)s, %(email)s, %(phone)s, %(status)s, %(notes)s, %(created_at)s, %(updated_at)s
        )
        ON CONFLICT (tenant_id) DO UPDATE SET
            name = EXCLUDED.name,
            email = EXCLUDED.email,
            phone = EXCLUDED.phone,
            status = EXCLUDED.status,
            notes = EXCLUDED.notes,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, data)
    return model


def get_tenant(tenant_id: TenantId) -> Optional[TenantModel]:
    row = _fetch_one(
        "SELECT * FROM tenants WHERE tenant_id = %(tenant_id)s",
        {"tenant_id": str(tenant_id)},
    )
    if not row:
        return None
    return TenantModel(**row)


def get_all_active_tenants() -> List[TenantModel]:
    rows = _fetch_all(
        "SELECT * FROM tenants WHERE status = 'active'",
        {},
    )
    return [TenantModel(**row) for row in rows]


def get_all_tenants() -> List[TenantModel]:
    rows = _fetch_all("SELECT * FROM tenants", {})
    return [TenantModel(**row) for row in rows]


def upsert_broker_account(model: BrokerAccountModel) -> BrokerAccountModel:
    data = model.model_dump()
    data["default_strategies"] = json.dumps(data.get("default_strategies") or [])
    data["meta"] = json.dumps(data.get("meta") or {})
    sql = """
        INSERT INTO broker_accounts (
            broker_account_id, tenant_id, broker_type, display_name, client_code, secret_ref,
            trading_mode, enabled, default_strategies, meta, created_at, updated_at
        ) VALUES (
            %(broker_account_id)s, %(tenant_id)s, %(broker_type)s, %(display_name)s, %(client_code)s, %(secret_ref)s,
            %(trading_mode)s, %(enabled)s, %(default_strategies)s, %(meta)s, %(created_at)s, %(updated_at)s
        )
        ON CONFLICT (broker_account_id) DO UPDATE SET
            tenant_id = EXCLUDED.tenant_id,
            broker_type = EXCLUDED.broker_type,
            display_name = EXCLUDED.display_name,
            client_code = EXCLUDED.client_code,
            secret_ref = EXCLUDED.secret_ref,
            trading_mode = EXCLUDED.trading_mode,
            enabled = EXCLUDED.enabled,
            default_strategies = EXCLUDED.default_strategies,
            meta = EXCLUDED.meta,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, data)
    return model


def get_broker_account(
    broker_account_id: BrokerAccountId,
) -> Optional[BrokerAccountModel]:
    row = _fetch_one(
        "SELECT * FROM broker_accounts WHERE broker_account_id = %(broker_account_id)s",
        {"broker_account_id": str(broker_account_id)},
    )
    if not row:
        return None
    return BrokerAccountModel(**_normalize_broker_account_row(row))


def get_broker_account_by_client_code(client_code: str) -> Optional[BrokerAccountModel]:
    if not client_code:
        return None
    row = _fetch_one(
        "SELECT * FROM broker_accounts WHERE client_code = %(client_code)s",
        {"client_code": client_code},
    )
    if not row:
        return None
    return BrokerAccountModel(**_normalize_broker_account_row(row))


def get_broker_accounts_for_tenant(tenant_id: TenantId) -> List[BrokerAccountModel]:
    rows = _fetch_all(
        "SELECT * FROM broker_accounts WHERE tenant_id = %(tenant_id)s",
        {"tenant_id": str(tenant_id)},
    )
    return [BrokerAccountModel(**_normalize_broker_account_row(row)) for row in rows]


def get_all_broker_accounts() -> List[BrokerAccountModel]:
    rows = _fetch_all("SELECT * FROM broker_accounts", {})
    return [BrokerAccountModel(**_normalize_broker_account_row(row)) for row in rows]


def get_enabled_broker_accounts() -> List[BrokerAccountModel]:
    rows = _fetch_all(
        "SELECT * FROM broker_accounts WHERE enabled = TRUE",
        {},
    )
    return [BrokerAccountModel(**_normalize_broker_account_row(row)) for row in rows]


def get_active_broker_accounts() -> List[BrokerAccountModel]:
    sql = """
        SELECT DISTINCT ba.*
        FROM broker_accounts ba
        JOIN tenants t ON t.tenant_id = ba.tenant_id
        JOIN subscriptions s ON s.broker_account_id = ba.broker_account_id
        WHERE ba.enabled = TRUE
          AND t.status = 'active'
          AND s.start_at <= NOW()
          AND s.end_at >= NOW()
    """
    rows = _fetch_all(sql, {})
    return [BrokerAccountModel(**_normalize_broker_account_row(row)) for row in rows]


def upsert_subscription(model: SubscriptionModel) -> SubscriptionModel:
    data = model.model_dump()
    sql = """
        INSERT INTO subscriptions (
            subscription_id, tenant_id, broker_account_id, mode, start_at, end_at, created_at, updated_at
        ) VALUES (
            %(subscription_id)s, %(tenant_id)s, %(broker_account_id)s, %(mode)s, %(start_at)s, %(end_at)s, %(created_at)s, %(updated_at)s
        )
        ON CONFLICT (subscription_id) DO UPDATE SET
            tenant_id = EXCLUDED.tenant_id,
            broker_account_id = EXCLUDED.broker_account_id,
            mode = EXCLUDED.mode,
            start_at = EXCLUDED.start_at,
            end_at = EXCLUDED.end_at,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, data)
    return model


def get_subscriptions_for_account(
    broker_account_id: BrokerAccountId,
) -> List[SubscriptionModel]:
    rows = _fetch_all(
        "SELECT * FROM subscriptions WHERE broker_account_id = %(broker_account_id)s",
        {"broker_account_id": str(broker_account_id)},
    )
    return [SubscriptionModel(**row) for row in rows]


def get_all_subscriptions() -> List[SubscriptionModel]:
    rows = _fetch_all("SELECT * FROM subscriptions", {})
    return [SubscriptionModel(**row) for row in rows]


def upsert_strategy_config(model: StrategyConfigModel) -> StrategyConfigModel:
    data = model.model_dump()
    data["params"] = json.dumps(data.get("params") or {})
    sql = """
        INSERT INTO strategy_configs (
            strategy_config_id, tenant_id, broker_account_id, strategy_id, enabled, params, created_at, updated_at
        ) VALUES (
            %(strategy_config_id)s, %(tenant_id)s, %(broker_account_id)s, %(strategy_id)s, %(enabled)s, %(params)s, %(created_at)s, %(updated_at)s
        )
        ON CONFLICT (strategy_config_id) DO UPDATE SET
            tenant_id = EXCLUDED.tenant_id,
            broker_account_id = EXCLUDED.broker_account_id,
            strategy_id = EXCLUDED.strategy_id,
            enabled = EXCLUDED.enabled,
            params = EXCLUDED.params,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, data)
    return model


def get_strategy_configs_for_account(
    broker_account_id: BrokerAccountId,
) -> List[StrategyConfigModel]:
    rows = _fetch_all(
        "SELECT * FROM strategy_configs WHERE broker_account_id = %(broker_account_id)s",
        {"broker_account_id": str(broker_account_id)},
    )
    return [StrategyConfigModel(**_normalize_strategy_config_row(row)) for row in rows]


def get_strategy_configs_for_tenant(tenant_id: TenantId) -> List[StrategyConfigModel]:
    rows = _fetch_all(
        "SELECT * FROM strategy_configs WHERE tenant_id = %(tenant_id)s",
        {"tenant_id": str(tenant_id)},
    )
    return [StrategyConfigModel(**_normalize_strategy_config_row(row)) for row in rows]


def get_strategy_configs_for_tenant_strategy(
    tenant_id: TenantId, strategy_id: str
) -> List[StrategyConfigModel]:
    rows = _fetch_all(
        "SELECT * FROM strategy_configs WHERE tenant_id = %(tenant_id)s AND strategy_id = %(strategy_id)s",
        {"tenant_id": str(tenant_id), "strategy_id": str(strategy_id)},
    )
    return [StrategyConfigModel(**_normalize_strategy_config_row(row)) for row in rows]


__all__ = [
    "upsert_tenant",
    "get_tenant",
    "get_all_active_tenants",
    "get_all_tenants",
    "upsert_broker_account",
    "get_broker_account",
    "get_broker_account_by_client_code",
    "get_broker_accounts_for_tenant",
    "get_all_broker_accounts",
    "get_enabled_broker_accounts",
    "get_active_broker_accounts",
    "upsert_subscription",
    "get_subscriptions_for_account",
    "get_all_subscriptions",
    "upsert_strategy_config",
    "get_strategy_configs_for_account",
    "get_strategy_configs_for_tenant",
    "get_strategy_configs_for_tenant_strategy",
]
