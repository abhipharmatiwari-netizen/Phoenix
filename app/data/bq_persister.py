"""
BigQuery persister for multi-tenant hub data.

This is intentionally separate from the legacy core/bar_persister.py and
core/trade_persister.py modules used by the single-tenant engine.

Here we provide helpers to insert:
- order records,
- trade records,
- daily PnL snapshots.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import logging
import os
from pathlib import Path

try:  # Optional dependency for local runs without BigQuery support.
    from google.cloud import bigquery  # type: ignore
    from google.auth.exceptions import DefaultCredentialsError  # type: ignore
except Exception:  # pragma: no cover - optional dependency in local/dev
    bigquery = None
    DefaultCredentialsError = Exception  # type: ignore

from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId, TenantId

logger = logging.getLogger(__name__)


# Detect Cloud Run environment (ADC available via service account).
def _running_in_cloud_run() -> bool:
    return bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION"))


# Resolve effective project id using settings or standard env vars.
def _effective_project_id() -> str:
    settings = get_settings()
    return (
        settings.gcp_project_id
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or ""
    )


# Normalize a BigQuery table id into a fully qualified string.
def _resolve_table_id(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) == 3:
        return raw
    project_id = _effective_project_id()
    if len(parts) == 2:
        if not project_id:
            return None
        return f"{project_id}.{raw}"
    settings = get_settings()
    if not project_id or not settings.bq_dataset_id:
        return None
    return f"{project_id}.{settings.bq_dataset_id}.{raw}"


def _trade_csv_base_dir() -> Path:
    configured = str(os.getenv("TRADE_CSV_PATH", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve().parent
    return Path(__file__).resolve().parents[2] / "logs"


def _parse_trade_timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_trade_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    for key in ("qty",):
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            record[key] = int(float(value))
        except Exception:
            continue
    for key in ("price", "realized_pnl", "fees", "entry_price", "exit_price"):
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            record[key] = float(value)
        except Exception:
            continue
    return record


def _list_trade_csv_candidates(base_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    direct_file = base_dir / "trades.csv"
    if direct_file.is_file():
        candidates.append(direct_file)

    if base_dir.is_dir():
        daily_dirs = sorted(
            [path for path in base_dir.iterdir() if path.is_dir()],
            key=lambda path: path.name,
            reverse=True,
        )
        for daily_dir in daily_dirs[:14]:
            path = daily_dir / "trades.csv"
            if path.is_file():
                candidates.append(path)
    return candidates


def _load_trade_rows_from_csv(
    *,
    tenant_id: TenantId,
    broker_account_id: Optional[BrokerAccountId] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 500,
) -> List[dict]:
    base_dir = _trade_csv_base_dir()
    rows: list[dict[str, Any]] = []
    tenant_text = str(tenant_id)
    broker_text = str(broker_account_id) if broker_account_id is not None else None
    start_utc = (
        start_time.astimezone(timezone.utc)
        if start_time is not None and start_time.tzinfo is not None
        else start_time.replace(tzinfo=timezone.utc)
        if start_time is not None
        else None
    )
    end_utc = (
        end_time.astimezone(timezone.utc)
        if end_time is not None and end_time.tzinfo is not None
        else end_time.replace(tzinfo=timezone.utc)
        if end_time is not None
        else None
    )

    for path in _list_trade_csv_candidates(base_dir):
        try:
            with open(path, mode="r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for raw_row in reader:
                    row = _coerce_trade_csv_row(raw_row)
                    if str(row.get("tenant_id") or "").strip() != tenant_text:
                        continue
                    if (
                        broker_text is not None
                        and str(row.get("broker_account_id") or "").strip() != broker_text
                    ):
                        continue
                    trade_dt = _parse_trade_timestamp(
                        row.get("trade_time")
                        or row.get("exit_time")
                        or row.get("entry_time")
                    )
                    if start_utc is not None and (
                        trade_dt is None or trade_dt < start_utc
                    ):
                        continue
                    if end_utc is not None and (
                        trade_dt is None or trade_dt > end_utc
                    ):
                        continue
                    row["_sort_ts"] = trade_dt or datetime.min.replace(
                        tzinfo=timezone.utc
                    )
                    rows.append(row)
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed to read trade CSV fallback %s: %s", path, exc)

    rows.sort(key=lambda row: row["_sort_ts"], reverse=True)
    trimmed = rows[: max(1, int(limit))]
    return [{k: v for k, v in row.items() if k != "_sort_ts"} for row in trimmed]


# Build and cache a BigQuery client.
@lru_cache(maxsize=1)
def get_bq_client() -> Optional["bigquery.Client"]:
    if bigquery is None:
        logger.info("BigQuery disabled: google-cloud-bigquery not installed.")
        return None
    settings = get_settings()
    try:
        return bigquery.Client(project=settings.gcp_project_id or None)
    except DefaultCredentialsError as exc:
        logger.warning("BigQuery disabled: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover - safety net
        logger.warning("BigQuery disabled: failed to init client (%s)", exc)
        return None


# Resolve the BigQuery orders table id from settings.
def _orders_table_id() -> Optional[str]:
    settings = get_settings()
    raw = settings.bq_orders_table or settings.bigquery_trades_table
    return _resolve_table_id(raw)


# Resolve the BigQuery trades table id from settings.
def _trades_table_id() -> Optional[str]:
    settings = get_settings()
    raw = settings.bq_trades_table or settings.bigquery_trades_table
    return _resolve_table_id(raw)


# Resolve the BigQuery daily PnL table id from settings.
def _daily_pnl_table_id() -> Optional[str]:
    settings = get_settings()
    raw = settings.bq_daily_pnl_table or settings.bigquery_pnl_table
    return _resolve_table_id(raw)


def _try_enqueue_async(
    table: str,
    record: Dict[str, Any],
    *,
    insert_id: Optional[str] = None,
) -> bool:
    try:
        from app.data.bq_async_writer import enqueue_record, is_bq_async_enabled
    except Exception:
        return False
    if not is_bq_async_enabled():
        return False
    return enqueue_record(table_id=table, row=record, insert_id=insert_id)


def _insert_rows_json(
    client,
    table: str,
    rows: List[Dict[str, Any]],
    *,
    row_ids: Optional[List[Optional[str]]] = None,
):
    if row_ids and any(rid is not None for rid in row_ids):
        try:
            return client.insert_rows_json(table, rows, row_ids=row_ids)
        except TypeError:
            # Compatibility for client stubs/mocks that don't accept row_ids.
            return client.insert_rows_json(table, rows)
    return client.insert_rows_json(table, rows)


# Insert a single order record into BigQuery.
def insert_order_record(
    record: Dict[str, Any],
    *,
    insert_id: Optional[str] = None,
) -> None:
    """
    Insert a single order record into the orders table.

    'record' should already be a flat dict matching the table schema.
    """
    table = _orders_table_id()
    if not table:
        return
    resolved_insert_id = (
        str(
            insert_id
            or record.get("hub_order_id")
            or record.get("broker_order_id")
            or ""
        ).strip()
        or None
    )
    if _try_enqueue_async(table, record, insert_id=resolved_insert_id):
        return
    client = get_bq_client()
    if client is None:
        return
    errors = _insert_rows_json(
        client,
        table,
        [record],
        row_ids=[resolved_insert_id] if resolved_insert_id else None,
    )
    if errors:
        logger.error("Failed to insert order record into %s: %s", table, errors)


# Insert a single trade record into BigQuery.
def insert_trade_record(
    record: Dict[str, Any],
    *,
    insert_id: Optional[str] = None,
) -> None:
    """
    Insert a single trade record into the trades table.
    Caller must pass a dict matching the table schema (including entry_price,
    exit_price, entry_time, exit_time).
    """
    table = _trades_table_id()
    if not table:
        return
    resolved_insert_id = str(insert_id or record.get("trade_id") or "").strip() or None
    if _try_enqueue_async(table, record, insert_id=resolved_insert_id):
        return
    client = get_bq_client()
    if client is None:
        return
    errors = _insert_rows_json(
        client,
        table,
        [record],
        row_ids=[resolved_insert_id] if resolved_insert_id else None,
    )
    if errors:
        logger.error("Failed to insert trade record into %s: %s", table, errors)


# Insert a daily PnL snapshot into BigQuery.
def insert_daily_pnl_snapshot(record: Dict[str, Any]) -> None:
    """
    Insert a single PnL snapshot record into the daily PnL table.
    """
    table = _daily_pnl_table_id()
    if not table:
        return
    if _try_enqueue_async(table, record):
        return
    client = get_bq_client()
    if client is None:
        return
    errors = client.insert_rows_json(table, [record])
    if errors:
        logger.error("Failed to insert daily PnL record into %s: %s", table, errors)


# Query recent trades for a tenant with optional filters.
def fetch_trades_for_tenant(
    tenant_id: TenantId,
    broker_account_id: Optional[BrokerAccountId] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 500,
) -> List[dict]:
    """
    Fetch trades for a given tenant (and optional broker account/time window)
    from the trades table.

    This is primarily used by tenant dashboard APIs.
    """
    table = _trades_table_id()
    if not table:
        return _load_trade_rows_from_csv(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    client = get_bq_client()
    if client is None:
        csv_rows = _load_trade_rows_from_csv(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        if csv_rows or not _running_in_cloud_run():
            return csv_rows
        return []

    where_clauses = ["tenant_id = @tenant_id"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("tenant_id", "STRING", str(tenant_id)),
    ]

    if broker_account_id is not None:
        where_clauses.append("broker_account_id = @broker_account_id")
        params.append(
            bigquery.ScalarQueryParameter(
                "broker_account_id", "STRING", str(broker_account_id)
            )
        )

    if start_time is not None:
        where_clauses.append("trade_time >= @start_time")
        params.append(
            bigquery.ScalarQueryParameter("start_time", "TIMESTAMP", start_time),
        )

    if end_time is not None:
        where_clauses.append("trade_time <= @end_time")
        params.append(
            bigquery.ScalarQueryParameter("end_time", "TIMESTAMP", end_time),
        )

    where_sql = " AND ".join(where_clauses)
    query = f"SELECT * FROM `{table}` WHERE {where_sql} ORDER BY trade_time DESC LIMIT @limit"

    params.append(
        bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
    )

    try:
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        query_job = client.query(query, job_config=job_config)
        rows = query_job.result()
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.warning("BigQuery trade query failed; falling back to CSV: %s", exc)
        return _load_trade_rows_from_csv(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )


__all__ = [
    "get_bq_client",
    "insert_order_record",
    "insert_trade_record",
    "insert_daily_pnl_snapshot",
    "fetch_trades_for_tenant",
]
