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
    "strategy_config_candidates",
    "users",
}
_SWEEP_TABLES = {"sweep_states", "eod_states"}
_LIFECYCLE_TABLES = {"trade_processed_markers"}
_KILL_SWITCH_TABLES = {"kill_switch_state"}
_OUTBOX_TABLES = {"order_submission_outbox"}
_OWNERSHIP_TABLES = {"position_ownership_ledger"}
_POSITION_RECORD_TABLES = {"internal_position_records"}
_REQUIRED_INDEXES = {
    "idx_broker_accounts_client_code",
    "idx_subscriptions_tenant_account_mode",
    "idx_users_email",
    "idx_strategy_config_candidates_cfg_status",
    "idx_strategy_config_candidates_created_at",
    "position_ownership_ledger_acct_idx",
    "idx_internal_position_records_active",
    "idx_kill_switch_state_active",
}
_REQUIRED_COLUMNS = {
    "broker_credentials": {
        "broker_account_id",
        "api_key",
        "api_secret",
        "client_code",
        "pin",
        "totp_secret",
        "client_local_ip",
        "client_public_ip",
        "mac_address",
        "state",
        "created_at",
        "updated_at",
    },
    "kill_switch_state": {
        "id",
        "scope",
        "scope_id",
        "state",
        "tripped_at",
        "tripped_by",
        "trip_reason",
        "cleared_at",
        "cleared_by",
        "clear_reason",
        "clear_request_id",
        "updated_at",
        "created_at",
        "block_exits",
    },
}


@dataclass(frozen=True)
class SchemaGuardResult:
    missing_tables: tuple[str, ...]
    missing_indexes: tuple[str, ...]
    missing_columns: tuple[str, ...] = ()


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
        tables.add("broker_credentials")
        if _order_lifecycle_markers_enabled():
            tables.update(_LIFECYCLE_TABLES)
        tables.update(_KILL_SWITCH_TABLES)
        tables.update(_OUTBOX_TABLES)
        tables.update(_OWNERSHIP_TABLES)
        tables.update(_POSITION_RECORD_TABLES)
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


def _fetch_existing_columns(conn) -> dict[str, set[str]]:
    sql = """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    columns: dict[str, set[str]] = {}
    for row in rows or []:
        table = str(row[0]).strip().lower()
        column = str(row[1]).strip().lower()
        if table and column:
            columns.setdefault(table, set()).add(column)
    return columns


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
            existing_columns = _fetch_existing_columns(conn)
    except Exception as exc:
        text = f"Schema guard failed to inspect Postgres schema: {exc}"
        if check_mode == "strict":
            raise RuntimeError(text) from exc
        logger.warning(text)
        return SchemaGuardResult(
            missing_tables=tuple(sorted(required_tables)),
            missing_indexes=(),
            missing_columns=(),
        )

    missing_tables = tuple(sorted(t for t in required_tables if t not in existing_tables))
    missing_indexes = tuple(
        sorted(i for i in _REQUIRED_INDEXES if i.lower() not in existing_indexes)
    )
    missing_columns = tuple(
        sorted(
            f"{table}.{column}"
            for table, required_columns in _REQUIRED_COLUMNS.items()
            if table in required_tables and table not in missing_tables
            for column in required_columns
            if column.lower() not in existing_columns.get(table, set())
        )
    )
    if missing_tables or missing_indexes or missing_columns:
        details = []
        if missing_tables:
            details.append(f"missing_tables={','.join(missing_tables)}")
        if missing_indexes:
            details.append(f"missing_indexes={','.join(missing_indexes)}")
        if missing_columns:
            details.append(f"missing_columns={','.join(missing_columns)}")
        text = "Schema guard detected missing schema objects: " + " ".join(details)
        if check_mode == "strict":
            raise RuntimeError(text)
        logger.warning(text)
    else:
        logger.info("Schema guard passed for required Postgres tables/indexes/columns.")
    return SchemaGuardResult(
        missing_tables=missing_tables,
        missing_indexes=missing_indexes,
        missing_columns=missing_columns,
    )


__all__ = ["SchemaGuardResult", "check_startup_schema"]
