"""Persistent state management for EOD Exit logic.

Allows the EOD Exit Engine to avoid duplicate execution of EOD exits
upon crashes or container restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict
from zoneinfo import ZoneInfo
import logging

try:  # pragma: no cover - optional in lightweight test environments
    from google.cloud import firestore  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    firestore = None

try:  # pragma: no cover - optional in lightweight test environments
    from psycopg.rows import dict_row  # type: ignore
except Exception:  # pragma: no cover - optional in lightweight test environments
    dict_row = None

from app.core.identifiers import BrokerAccountId, TenantId
from app.data.postgres import connect_with_retry

logger = logging.getLogger(__name__)

@dataclass
class EODState:
    """Persistent state for EOD exit tracking."""
    tenant_id: str
    broker_account_id: str
    last_exit_date: Optional[str] = None  # YYYY-MM-DD
    
    @classmethod
    def from_firestore(cls, doc_data: Dict) -> EODState:
        return cls(
            tenant_id=doc_data.get("tenant_id", ""),
            broker_account_id=doc_data.get("broker_account_id", ""),
            last_exit_date=doc_data.get("last_exit_date"),
        )
    
    def to_firestore(self) -> Dict:
        return {
            "tenant_id": self.tenant_id,
            "broker_account_id": self.broker_account_id,
            "last_exit_date": self.last_exit_date,
            "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }

class EODStateStore:
    """Manages persistent EOD state in Firestore."""
    COLLECTION_NAME = "eod_states"
    
    def __init__(self, firestore_client, default_time_zone: str = "Asia/Kolkata"):
        self.firestore_client = firestore_client
        self._tz = ZoneInfo(default_time_zone)
    
    def _get_doc_id(self, tenant_id: TenantId, broker_account_id: BrokerAccountId) -> str:
        return f"{tenant_id}_{broker_account_id}"
    
    def get_state(self, tenant_id: TenantId, broker_account_id: BrokerAccountId) -> EODState:
        doc_id = self._get_doc_id(tenant_id, broker_account_id)
        try:
            doc = self.firestore_client.collection(self.COLLECTION_NAME).document(doc_id).get()
            if not doc.exists:
                state = EODState(str(tenant_id), str(broker_account_id))
                self.save_state(state)
                return state
            return EODState.from_firestore(doc.to_dict())
        except Exception as exc:
            logger.error(f"Failed to get EOD state for {tenant_id}/{broker_account_id}: {exc}")
            return EODState(str(tenant_id), str(broker_account_id))
            
    def save_state(self, state: EODState) -> None:
        doc_id = self._get_doc_id(state.tenant_id, state.broker_account_id)
        self.firestore_client.collection(self.COLLECTION_NAME).document(doc_id).set(state.to_firestore())

class PostgresEODStateStore:
    """Manages persistent EOD state in Postgres."""

    TABLE_NAME = "eod_states"

    def __init__(self, dsn: str, default_time_zone: str = "Asia/Kolkata"):
        if dict_row is None:
            raise RuntimeError("psycopg is required for Postgres EOD state store")
        self.dsn = dsn
        self._tz = ZoneInfo(default_time_zone)
        self._ensure_schema()

    def _conn(self):
        return connect_with_retry(self.dsn, autocommit=True)

    def _ensure_schema(self) -> None:
        statements = (
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                tenant_id TEXT NOT NULL,
                broker_account_id TEXT NOT NULL,
                last_exit_date DATE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant_id, broker_account_id)
            )
            """,
            f"""
            ALTER TABLE {self.TABLE_NAME}
                ADD COLUMN IF NOT EXISTS last_exit_date DATE
            """,
            f"""
            ALTER TABLE {self.TABLE_NAME}
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
            """,
            f"""
            UPDATE {self.TABLE_NAME}
            SET updated_at = NOW()
            WHERE updated_at IS NULL
            """,
            f"""
            ALTER TABLE {self.TABLE_NAME}
                ALTER COLUMN updated_at SET DEFAULT NOW()
            """,
            f"""
            ALTER TABLE {self.TABLE_NAME}
                ALTER COLUMN updated_at SET NOT NULL
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_eod_states_updated_at
                ON {self.TABLE_NAME}(updated_at)
            """,
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

    def get_state(self, tenant_id: TenantId, broker_account_id: BrokerAccountId) -> EODState:
        sql = (
            f"SELECT tenant_id, broker_account_id, last_exit_date "
            f"FROM {self.TABLE_NAME} "
            "WHERE tenant_id = %(tenant_id)s AND broker_account_id = %(broker_account_id)s"
        )
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, {"tenant_id": str(tenant_id), "broker_account_id": str(broker_account_id)})
                row = cur.fetchone()
        if not row:
            state = EODState(str(tenant_id), str(broker_account_id))
            self.save_state(state)
            return state
        last_date = row.get("last_exit_date")
        if last_date is not None:
            last_date = str(last_date)
        return EODState(row.get("tenant_id", ""), row.get("broker_account_id", ""), last_date)

    def save_state(self, state: EODState) -> None:
        sql = """
            INSERT INTO {table_name} (tenant_id, broker_account_id, last_exit_date, updated_at)
            VALUES (%(tenant_id)s, %(broker_account_id)s, %(last_exit_date)s, %(updated_at)s)
            ON CONFLICT (tenant_id, broker_account_id) DO UPDATE SET
                last_exit_date = EXCLUDED.last_exit_date,
                updated_at = EXCLUDED.updated_at
        """.format(table_name=self.TABLE_NAME)
        payload = {
            "tenant_id": state.tenant_id,
            "broker_account_id": state.broker_account_id,
            "last_exit_date": state.last_exit_date,
            "updated_at": datetime.now(ZoneInfo("UTC")),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)

class EODStateManager:
    """High-level manager for EOD state operations."""
    def __init__(self, state_store: EODStateStore | PostgresEODStateStore, default_time_zone: str = "Asia/Kolkata"):
        self.state_store = state_store
        self._tz = ZoneInfo(default_time_zone)
        
    def _get_today(self) -> str:
        now = datetime.now(self._tz)
        return now.strftime("%Y-%m-%d")

    def has_exited_today(self, tenant_id: TenantId, broker_account_id: BrokerAccountId) -> bool:
        state = self.state_store.get_state(tenant_id, broker_account_id)
        return state.last_exit_date == self._get_today()

    def record_eod_exit(self, tenant_id: TenantId, broker_account_id: BrokerAccountId) -> bool:
        try:
            state = self.state_store.get_state(tenant_id, broker_account_id)
            state.last_exit_date = self._get_today()
            self.state_store.save_state(state)
            logger.debug(f"Recorded EOD exit for {tenant_id}/{broker_account_id}")
            return True
        except Exception as exc:
            logger.error(f"Failed to record EOD exit for {tenant_id}/{broker_account_id}: {exc}", exc_info=True)
            return False
