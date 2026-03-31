from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from app.config.settings import get_settings
from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)


class CircuitBreakerStateStore(Protocol):
    def load_state(self) -> Optional[dict[str, Any]]: ...

    def save_state(self, state: dict[str, Any]) -> None: ...

    def clear_state(self) -> None: ...


def _safe_identifier(raw: str, default: str) -> str:
    text = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw or ""))
    text = text.strip("_")
    return text or default


class PostgresCircuitBreakerStateStore:
    def __init__(
        self,
        *,
        dsn: str,
        scope_id: str,
        table_name: str = "circuit_breaker_state",
    ) -> None:
        self._dsn = dsn
        self._scope_id = str(scope_id or "").strip() or "default"
        self._table = _safe_identifier(table_name, "circuit_breaker_state")
        self._ensure_schema()

    def _conn(self):
        return connect_with_retry(self._dsn, autocommit=False)

    def _ensure_schema(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                scope_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def load_state(self) -> Optional[dict[str, Any]]:
        query = f"""
            SELECT state_json
            FROM {self._table}
            WHERE scope_id = %(scope_id)s
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, {"scope_id": self._scope_id})
                row = cur.fetchone()
            conn.commit()
        if not row:
            return None
        try:
            parsed = json.loads(str(row[0] or "{}"))
        except json.JSONDecodeError:
            logger.warning(
                "circuit_breaker_state_store_invalid_json scope_id=%s",
                self._scope_id,
            )
            return None
        return parsed if isinstance(parsed, dict) else None

    def save_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=True, separators=(",", ":"))
        query = f"""
            INSERT INTO {self._table} (
                scope_id,
                state_json,
                updated_at
            ) VALUES (
                %(scope_id)s,
                %(state_json)s,
                %(updated_at)s
            )
            ON CONFLICT (scope_id) DO UPDATE
            SET state_json = EXCLUDED.state_json,
                updated_at = EXCLUDED.updated_at
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    {
                        "scope_id": self._scope_id,
                        "state_json": payload,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            conn.commit()

    def clear_state(self) -> None:
        query = f"DELETE FROM {self._table} WHERE scope_id = %(scope_id)s"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, {"scope_id": self._scope_id})
            conn.commit()


def build_circuit_breaker_state_store() -> Optional[CircuitBreakerStateStore]:
    settings = get_settings()
    if not bool(getattr(settings, "circuit_breaker_persist_state", False)):
        return None
    scope_id = (
        str(getattr(settings, "hub_instance_name", "") or "").strip()
        or str(os.getenv("K_SERVICE", "") or "").strip()
        or "default"
    )
    table_name = str(os.getenv("CIRCUIT_BREAKER_PERSIST_TABLE", "circuit_breaker_state") or "").strip() or "circuit_breaker_state"
    try:
        dsn = get_control_plane_dsn(settings)
        return PostgresCircuitBreakerStateStore(
            dsn=dsn,
            scope_id=scope_id,
            table_name=table_name,
        )
    except Exception as exc:
        logger.warning(
            "circuit_breaker_state_store_unavailable scope_id=%s error=%s",
            scope_id,
            exc,
        )
        return None


__all__ = [
    "CircuitBreakerStateStore",
    "PostgresCircuitBreakerStateStore",
    "build_circuit_breaker_state_store",
]
