"""Startup schema compatibility checks for Postgres control-plane tables."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Optional

from app.config.settings import Settings, get_settings
from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)

_CONTROL_PLANE_TABLES = {
    "tenants",
    "broker_accounts",
    "subscriptions",
    "strategy_configs",
    "users",
}
_SWEEP_TABLES = {"sweep_states", "eod_states"}
_LIFECYCLE_TABLES = {"trade_processed_markers"}
_KILL_SWITCH_TABLES = {"kill_switch_state"}
_OUTBOX_TABLES = {"order_submission_outbox"}
_OWNERSHIP_TABLES = {"position_ownership_ledger"}
_REQUIRED_INDEXES = {
    "idx_broker_accounts_client_code",
    "idx_subscriptions_tenant_account_mode",
    "idx_users_email",
    "position_ownership_ledger_acct_idx",
}


@dataclass(frozen=True)
class SchemaGuardResult:
    missing_tables: tuple[str, ...]
    missing_indexes: tuple[str, ...]


def _order_lifecycle_markers_enabled() -> bool:
    raw = str(os.getenv("ORDER_LIFECYCLE_PERSIST_MARKERS", "true")).strip().lower()
    return raw not in {"0", "false", "no"}


def _schema_mode(settings: Settings, override: Optional[str]) -> str:
    mode = str(
        override or getattr(settings, "schema_check_mode", "warn") or "warn"
    ).strip().lower()
    if mode not in {"warn", "strict"}:
        return "warn"
    return mode


def _required_tables(settings: Settings) -> set[str]:
    tables: set[str] = set()
    if str(getattr(settings, "control_plane_backend", "") or "").strip().lower() == "postgres":
        tables.update(_CONTROL_PLANE_TABLES)
        if _order_lifecycle_markers_enabled():
            tables.update(_LIFECYCLE_TABLES)
        tables.update(_KILL_SWITCH_TABLES)
        tables.update(_OUTBOX_TABLES)
        tables.update(_OWNERSHIP_TABLES)
    if str(getattr(settings, "sweep_state_backend", "") or "").strip().lower() == "postgres":
        tables.update(_SWEEP_TABLES)
    return tables


def _fetch_existing_tables(conn) -> set[str]:
    sql = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {str(row[0]).strip().lower() for row in rows or []}


def _fetch_existing_indexes(conn) -> set[str]:
    sql = """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {str(row[0]).strip().lower() for row in rows or []}


def check_startup_schema(
    *,
    settings: Optional[Settings] = None,
    mode: Optional[str] = None,
) -> SchemaGuardResult:
    cfg = settings or get_settings()
    check_mode = _schema_mode(cfg, mode)
    required_tables = _required_tables(cfg)
    if not required_tables:
        return SchemaGuardResult(missing_tables=(), missing_indexes=())

    try:
        dsn = get_control_plane_dsn(cfg)
        with connect_with_retry(
            dsn,
            autocommit=True,
            max_attempts=1,
            base_backoff_seconds=0.0,
        ) as conn:
            existing_tables = _fetch_existing_tables(conn)
            existing_indexes = _fetch_existing_indexes(conn)
    except Exception as exc:
        text = f"Schema guard failed to inspect Postgres schema: {exc}"
        if check_mode == "strict":
            raise RuntimeError(text) from exc
        logger.warning(text)
        return SchemaGuardResult(missing_tables=tuple(sorted(required_tables)), missing_indexes=())

    missing_tables = tuple(sorted(t for t in required_tables if t not in existing_tables))
    missing_indexes = tuple(
        sorted(i for i in _REQUIRED_INDEXES if i.lower() not in existing_indexes)
    )
    if missing_tables or missing_indexes:
        details = []
        if missing_tables:
            details.append(f"missing_tables={','.join(missing_tables)}")
        if missing_indexes:
            details.append(f"missing_indexes={','.join(missing_indexes)}")
        text = "Schema guard detected missing schema objects: " + " ".join(details)
        if check_mode == "strict":
            raise RuntimeError(text)
        logger.warning(text)
    else:
        logger.info("Schema guard passed for required Postgres tables/indexes.")
    return SchemaGuardResult(
        missing_tables=missing_tables,
        missing_indexes=missing_indexes,
    )


__all__ = ["SchemaGuardResult", "check_startup_schema"]
