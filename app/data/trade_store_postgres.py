"""Postgres-backed per-fill trade ledger.

Source of truth for the /me/trades dashboard endpoint in deployments where
BigQuery is not configured. Mirrors the schema produced by
trade_record_to_bq_row(...) so the read path returns rows the frontend
already knows how to render.

Idempotent inserts via ON CONFLICT DO NOTHING on (broker_account_id,
trade_id) — a re-emitted fill notification for the same broker_order_id
is a no-op.

Failures are logged but never raise: this module is a write/read sidecar
to the canonical pnl_engine.on_trade() flow that updates pnl_snapshots.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.identifiers import BrokerAccountId, TenantId

logger = logging.getLogger(__name__)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.trades (
    trade_id          TEXT             NOT NULL,
    broker_account_id TEXT             NOT NULL,
    tenant_id         TEXT             NOT NULL,
    strategy_id       TEXT             NOT NULL,
    hub_order_id      TEXT,
    symbol            TEXT             NOT NULL,
    side              TEXT             NOT NULL,
    qty               BIGINT           NOT NULL,
    price             DOUBLE PRECISION NOT NULL,
    trade_time        TIMESTAMPTZ      NOT NULL,
    realized_pnl      DOUBLE PRECISION,
    fees              DOUBLE PRECISION,
    entry_price       DOUBLE PRECISION,
    exit_price        DOUBLE PRECISION,
    entry_time        TIMESTAMPTZ,
    exit_time         TIMESTAMPTZ,
    execution_mode    TEXT,
    virtual           BOOLEAN          NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (broker_account_id, trade_id)
);
"""

_INSERT = """
INSERT INTO public.trades (
    trade_id, broker_account_id, tenant_id, strategy_id, hub_order_id,
    symbol, side, qty, price, trade_time,
    realized_pnl, fees, entry_price, exit_price, entry_time, exit_time,
    execution_mode, virtual
)
VALUES (
    %(trade_id)s, %(broker_account_id)s, %(tenant_id)s, %(strategy_id)s,
    %(hub_order_id)s, %(symbol)s, %(side)s, %(qty)s, %(price)s, %(trade_time)s,
    %(realized_pnl)s, %(fees)s, %(entry_price)s, %(exit_price)s,
    %(entry_time)s, %(exit_time)s,
    %(execution_mode)s, %(virtual)s
)
ON CONFLICT (broker_account_id, trade_id) DO NOTHING;
"""

_SELECT_BASE = """
SELECT trade_id, broker_account_id, tenant_id, strategy_id, hub_order_id,
       symbol, side, qty, price, trade_time,
       realized_pnl, fees, entry_price, exit_price, entry_time, exit_time,
       execution_mode, virtual, created_at
FROM public.trades
"""


def _ensure_table(dsn: Optional[str] = None) -> bool:
    """Idempotent CREATE TABLE call. Returns True if the table is reachable."""
    if dsn is None:
        try:
            from app.data.postgres import get_control_plane_dsn
            dsn = get_control_plane_dsn()
        except Exception as exc:
            logger.debug("trade_store_postgres: cannot resolve DSN: %s", exc)
            return False
    try:
        from app.data.postgres import connect_with_retry
        with connect_with_retry(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_TABLE)
            conn.commit()
        return True
    except Exception as exc:
        logger.debug("trade_store_postgres: ensure_table failed: %s", exc)
        return False


def _coerce_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Map a record produced by trade_record_to_bq_row(...) into the shape
    expected by the parameterized INSERT above."""
    out: Dict[str, Any] = {
        "trade_id": str(record.get("trade_id") or "").strip(),
        "broker_account_id": str(record.get("broker_account_id") or "").strip(),
        "tenant_id": str(record.get("tenant_id") or "").strip(),
        "strategy_id": str(record.get("strategy_id") or "").strip(),
        "hub_order_id": str(record.get("hub_order_id") or "").strip() or None,
        "symbol": str(record.get("symbol") or "").strip(),
        "side": str(record.get("side") or "").strip().upper(),
        "qty": int(record.get("qty") or 0),
        "price": float(record.get("price") or 0.0),
        "trade_time": _coerce_dt(record.get("trade_time")),
        "realized_pnl": (
            float(record["realized_pnl"])
            if record.get("realized_pnl") is not None and record.get("realized_pnl") != ""
            else None
        ),
        "fees": (
            float(record["fees"])
            if record.get("fees") is not None and record.get("fees") != ""
            else None
        ),
        "entry_price": (
            float(record["entry_price"])
            if record.get("entry_price") is not None and record.get("entry_price") != ""
            else None
        ),
        "exit_price": (
            float(record["exit_price"])
            if record.get("exit_price") is not None and record.get("exit_price") != ""
            else None
        ),
        "entry_time": _coerce_dt(record.get("entry_time")),
        "exit_time": _coerce_dt(record.get("exit_time")),
        "execution_mode": str(record.get("execution_mode") or "").strip() or None,
        "virtual": bool(record.get("virtual") or False),
    }
    return out


def insert_trade_postgres(record: Dict[str, Any], *, dsn: Optional[str] = None) -> bool:
    """Insert one trade row. Returns True on success, False on any failure
    (failures are logged at WARNING level)."""
    payload = _normalize_record(record)
    if not payload["trade_id"] or not payload["broker_account_id"]:
        logger.warning(
            "trade_store_postgres: refusing insert with missing trade_id or "
            "broker_account_id (record=%r)",
            {k: v for k, v in payload.items() if k not in ("symbol",)},
        )
        return False
    if payload["trade_time"] is None:
        payload["trade_time"] = datetime.now(timezone.utc)
    if dsn is None:
        try:
            from app.data.postgres import get_control_plane_dsn
            dsn = get_control_plane_dsn()
        except Exception as exc:
            logger.warning("trade_store_postgres: cannot resolve DSN: %s", exc)
            return False
    try:
        from app.data.postgres import connect_with_retry
        with connect_with_retry(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT, payload)
            conn.commit()
        return True
    except Exception as exc:
        logger.warning(
            "trade_store_postgres: insert failed for trade_id=%s broker=%s: %s",
            payload.get("trade_id"),
            payload.get("broker_account_id"),
            exc,
        )
        return False


def fetch_trades_postgres(
    *,
    tenant_id: TenantId,
    broker_account_id: Optional[BrokerAccountId] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 500,
    dsn: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Return rows for /me/trades. Mirrors fetch_trades_for_tenant's
    interface. Returns None on a connection / query failure so the caller
    can fall through to the legacy CSV path; returns [] when the table is
    empty for the given filters."""
    if dsn is None:
        try:
            from app.data.postgres import get_control_plane_dsn
            dsn = get_control_plane_dsn()
        except Exception as exc:
            logger.debug("trade_store_postgres: cannot resolve DSN: %s", exc)
            return None
    where: List[str] = ["tenant_id = %s"]
    params: List[Any] = [str(tenant_id)]
    if broker_account_id is not None:
        where.append("broker_account_id = %s")
        params.append(str(broker_account_id))
    if start_time is not None:
        where.append("trade_time >= %s")
        params.append(
            start_time if start_time.tzinfo is not None else start_time.replace(tzinfo=timezone.utc)
        )
    if end_time is not None:
        where.append("trade_time <= %s")
        params.append(
            end_time if end_time.tzinfo is not None else end_time.replace(tzinfo=timezone.utc)
        )
    sql = (
        _SELECT_BASE
        + "WHERE "
        + " AND ".join(where)
        + " ORDER BY trade_time DESC LIMIT %s"
    )
    params.append(int(limit))
    try:
        from app.data.postgres import connect_with_retry
        with connect_with_retry(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        # Normalize datetimes to ISO strings to match BQ / CSV reader output.
        for row in rows:
            for key in ("trade_time", "entry_time", "exit_time", "created_at"):
                val = row.get(key)
                if isinstance(val, datetime):
                    row[key] = val.isoformat()
        return rows
    except Exception as exc:
        logger.warning("trade_store_postgres: fetch failed: %s", exc)
        return None


__all__ = [
    "fetch_trades_postgres",
    "insert_trade_postgres",
]
