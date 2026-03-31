"""
Durable/shared PnL snapshot stores.

Backends:
- in-memory (local/dev fallback)
- Firestore
- Postgres
- Redis
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, Optional, Protocol

from psycopg.rows import dict_row

from app.config.settings import Settings, get_settings
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.data.postgres import connect_with_retry, get_control_plane_dsn
from app.pnl.types import PnLSnapshot, PnLSnapshotKey

logger = logging.getLogger(__name__)


class PnLStateStore(Protocol):
    def get_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[PnLSnapshot]: ...

    def upsert_snapshot(self, snapshot: PnLSnapshot) -> None: ...

    def list_account_snapshots(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> list[PnLSnapshot]: ...


class InMemoryPnLStateStore:
    def __init__(self) -> None:
        self._snapshots: Dict[tuple[TenantId, BrokerAccountId, StrategyId], PnLSnapshot] = {}

    def get_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[PnLSnapshot]:
        return self._snapshots.get((tenant_id, broker_account_id, strategy_id))

    def upsert_snapshot(self, snapshot: PnLSnapshot) -> None:
        key = (
            snapshot.key.tenant_id,
            snapshot.key.broker_account_id,
            snapshot.key.strategy_id,
        )
        self._snapshots[key] = snapshot

    def list_account_snapshots(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> list[PnLSnapshot]:
        return [
            snap
            for (t_id, b_id, _), snap in self._snapshots.items()
            if t_id == tenant_id and b_id == broker_account_id
        ]


class FirestorePnLStateStore:
    def __init__(self, *, client, collection_name: str) -> None:
        self._collection = client.collection(collection_name)

    @staticmethod
    def _doc_id(
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> str:
        return f"{tenant_id}::{broker_account_id}::{strategy_id}".replace("/", "_")

    @staticmethod
    def _serialize(snapshot: PnLSnapshot) -> Dict[str, Any]:
        return {
            "tenant_id": str(snapshot.key.tenant_id),
            "broker_account_id": str(snapshot.key.broker_account_id),
            "strategy_id": str(snapshot.key.strategy_id),
            "realized_pnl": float(snapshot.realized_pnl or 0.0),
            "unrealized_pnl": float(snapshot.unrealized_pnl or 0.0),
            "gross_exposure": float(snapshot.gross_exposure or 0.0),
            "as_of": snapshot.as_of.isoformat() if snapshot.as_of else None,
            "session_date": (
                snapshot.session_date.isoformat() if snapshot.session_date else None
            ),
            "updated_at": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def _parse_date(raw: object) -> Optional[date]:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except Exception:
            return None

    @staticmethod
    def _parse_dt(raw: object) -> Optional[datetime]:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _deserialize(self, doc_data: Dict[str, Any]) -> Optional[PnLSnapshot]:
        try:
            key = PnLSnapshotKey(
                tenant_id=TenantId(str(doc_data.get("tenant_id", ""))),
                broker_account_id=BrokerAccountId(
                    str(doc_data.get("broker_account_id", ""))
                ),
                strategy_id=StrategyId(str(doc_data.get("strategy_id", ""))),
            )
            if not key.tenant_id or not key.broker_account_id or not key.strategy_id:
                return None
            return PnLSnapshot(
                key=key,
                realized_pnl=float(doc_data.get("realized_pnl") or 0.0),
                unrealized_pnl=float(doc_data.get("unrealized_pnl") or 0.0),
                gross_exposure=float(doc_data.get("gross_exposure") or 0.0),
                as_of=self._parse_dt(doc_data.get("as_of")),
                session_date=self._parse_date(doc_data.get("session_date")),
            )
        except Exception:
            return None

    def get_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[PnLSnapshot]:
        try:
            doc = self._collection.document(
                self._doc_id(tenant_id, broker_account_id, strategy_id)
            ).get()
            if not doc.exists:
                return None
            return self._deserialize(doc.to_dict() or {})
        except Exception as exc:
            logger.warning(
                "FirestorePnLStateStore.get_snapshot failed tenant=%s account=%s strategy=%s err=%s",
                tenant_id,
                broker_account_id,
                strategy_id,
                exc,
            )
            return None

    def upsert_snapshot(self, snapshot: PnLSnapshot) -> None:
        payload = self._serialize(snapshot)
        try:
            self._collection.document(
                self._doc_id(
                    snapshot.key.tenant_id,
                    snapshot.key.broker_account_id,
                    snapshot.key.strategy_id,
                )
            ).set(payload, merge=True)
        except Exception as exc:
            logger.warning(
                "FirestorePnLStateStore.upsert_snapshot failed tenant=%s account=%s strategy=%s err=%s",
                snapshot.key.tenant_id,
                snapshot.key.broker_account_id,
                snapshot.key.strategy_id,
                exc,
            )

    def list_account_snapshots(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> list[PnLSnapshot]:
        try:
            docs = (
                self._collection.where(
                    field_path="tenant_id",
                    op_string="==",
                    value=str(tenant_id),
                )
                .where(
                    field_path="broker_account_id",
                    op_string="==",
                    value=str(broker_account_id),
                )
                .stream()
            )
            result: list[PnLSnapshot] = []
            for doc in docs:
                snap = self._deserialize(doc.to_dict() or {})
                if snap is not None:
                    result.append(snap)
            return result
        except Exception as exc:
            logger.warning(
                "FirestorePnLStateStore.list_account_snapshots failed tenant=%s account=%s err=%s",
                tenant_id,
                broker_account_id,
                exc,
            )
            return []


def _safe_identifier(raw: str, default: str) -> str:
    text = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw or ""))
    text = text.strip("_")
    return text or default


class PostgresPnLStateStore:
    def __init__(self, *, dsn: str, table_name: str = "pnl_snapshots") -> None:
        self._dsn = dsn
        self._table = _safe_identifier(table_name, "pnl_snapshots")
        self._ensure_schema()

    def _conn(self):
        return connect_with_retry(self._dsn, autocommit=True)

    def _ensure_schema(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                tenant_id TEXT NOT NULL,
                broker_account_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                unrealized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                gross_exposure DOUBLE PRECISION NOT NULL DEFAULT 0,
                as_of TIMESTAMPTZ,
                session_date DATE,
                freshness_updated_at TIMESTAMPTZ,
                freshness_source TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant_id, broker_account_id, strategy_id)
            )
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                # Migrate existing tables that lack freshness columns.
                for col, col_type in [
                    ("freshness_updated_at", "TIMESTAMPTZ"),
                    ("freshness_source", "TEXT"),
                ]:
                    cur.execute(f"""
                        DO $$ BEGIN
                            ALTER TABLE {self._table} ADD COLUMN {col} {col_type};
                        EXCEPTION WHEN duplicate_column THEN NULL;
                        END $$;
                    """)
                # Backfill any legacy rows that have NULL freshness.
                cur.execute(f"""
                    UPDATE {self._table}
                    SET freshness_updated_at = COALESCE(as_of, updated_at, NOW()),
                        freshness_source = 'schema_migration_backfill'
                    WHERE freshness_updated_at IS NULL
                """)

    @staticmethod
    def _row_to_snapshot(row: Dict[str, Any]) -> PnLSnapshot:
        return PnLSnapshot(
            key=PnLSnapshotKey(
                tenant_id=TenantId(str(row.get("tenant_id", ""))),
                broker_account_id=BrokerAccountId(str(row.get("broker_account_id", ""))),
                strategy_id=StrategyId(str(row.get("strategy_id", ""))),
            ),
            realized_pnl=float(row.get("realized_pnl") or 0.0),
            unrealized_pnl=float(row.get("unrealized_pnl") or 0.0),
            gross_exposure=float(row.get("gross_exposure") or 0.0),
            as_of=row.get("as_of"),
            session_date=row.get("session_date"),
            freshness_updated_at=row.get("freshness_updated_at"),
            freshness_source=row.get("freshness_source"),
        )

    def get_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[PnLSnapshot]:
        sql = f"""
            SELECT tenant_id, broker_account_id, strategy_id,
                   realized_pnl, unrealized_pnl, gross_exposure, as_of, session_date,
                   freshness_updated_at, freshness_source
            FROM {self._table}
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND strategy_id = %(strategy_id)s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    sql,
                    {
                        "tenant_id": str(tenant_id),
                        "broker_account_id": str(broker_account_id),
                        "strategy_id": str(strategy_id),
                    },
                )
                row = cur.fetchone()
        if not row:
            return None
        return self._row_to_snapshot(row)

    def upsert_snapshot(self, snapshot: PnLSnapshot) -> None:
        sql = f"""
            INSERT INTO {self._table} (
                tenant_id, broker_account_id, strategy_id,
                realized_pnl, unrealized_pnl, gross_exposure, as_of, session_date,
                freshness_updated_at, freshness_source, updated_at
            ) VALUES (
                %(tenant_id)s, %(broker_account_id)s, %(strategy_id)s,
                %(realized_pnl)s, %(unrealized_pnl)s, %(gross_exposure)s, %(as_of)s, %(session_date)s,
                %(freshness_updated_at)s, %(freshness_source)s, NOW()
            )
            ON CONFLICT (tenant_id, broker_account_id, strategy_id) DO UPDATE SET
                realized_pnl = EXCLUDED.realized_pnl,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                gross_exposure = EXCLUDED.gross_exposure,
                as_of = EXCLUDED.as_of,
                session_date = EXCLUDED.session_date,
                freshness_updated_at = EXCLUDED.freshness_updated_at,
                freshness_source = EXCLUDED.freshness_source,
                updated_at = NOW()
        """
        payload = {
            "tenant_id": str(snapshot.key.tenant_id),
            "broker_account_id": str(snapshot.key.broker_account_id),
            "strategy_id": str(snapshot.key.strategy_id),
            "realized_pnl": float(snapshot.realized_pnl or 0.0),
            "unrealized_pnl": float(snapshot.unrealized_pnl or 0.0),
            "gross_exposure": float(snapshot.gross_exposure or 0.0),
            "as_of": snapshot.as_of,
            "session_date": snapshot.session_date,
            "freshness_updated_at": snapshot.freshness_updated_at,
            "freshness_source": snapshot.freshness_source,
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)

    def list_account_snapshots(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> list[PnLSnapshot]:
        sql = f"""
            SELECT tenant_id, broker_account_id, strategy_id,
                   realized_pnl, unrealized_pnl, gross_exposure, as_of, session_date,
                   freshness_updated_at, freshness_source
            FROM {self._table}
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    sql,
                    {
                        "tenant_id": str(tenant_id),
                        "broker_account_id": str(broker_account_id),
                    },
                )
                rows = list(cur.fetchall())
        return [self._row_to_snapshot(row) for row in rows]


class RedisPnLStateStore:
    def __init__(self, *, redis_client, key_prefix: str) -> None:
        self._redis = redis_client
        self._prefix = str(key_prefix or "pnl_snapshot").strip() or "pnl_snapshot"

    def _key(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> str:
        return f"{self._prefix}:{tenant_id}:{broker_account_id}:{strategy_id}"

    @staticmethod
    def _serialize(snapshot: PnLSnapshot) -> str:
        payload = {
            "tenant_id": str(snapshot.key.tenant_id),
            "broker_account_id": str(snapshot.key.broker_account_id),
            "strategy_id": str(snapshot.key.strategy_id),
            "realized_pnl": float(snapshot.realized_pnl or 0.0),
            "unrealized_pnl": float(snapshot.unrealized_pnl or 0.0),
            "gross_exposure": float(snapshot.gross_exposure or 0.0),
            "as_of": snapshot.as_of.isoformat() if snapshot.as_of else None,
            "session_date": (
                snapshot.session_date.isoformat() if snapshot.session_date else None
            ),
        }
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _deserialize(raw: object) -> Optional[PnLSnapshot]:
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", errors="ignore")
        else:
            text = str(raw)
        try:
            data = json.loads(text)
        except Exception:
            return None
        try:
            as_of_raw = data.get("as_of")
            as_of = None
            if as_of_raw:
                as_of_text = str(as_of_raw)
                if as_of_text.endswith("Z"):
                    as_of_text = as_of_text[:-1] + "+00:00"
                as_of = datetime.fromisoformat(as_of_text)
            session_raw = data.get("session_date")
            session_date = date.fromisoformat(session_raw) if session_raw else None
            return PnLSnapshot(
                key=PnLSnapshotKey(
                    tenant_id=TenantId(str(data.get("tenant_id", ""))),
                    broker_account_id=BrokerAccountId(
                        str(data.get("broker_account_id", ""))
                    ),
                    strategy_id=StrategyId(str(data.get("strategy_id", ""))),
                ),
                realized_pnl=float(data.get("realized_pnl") or 0.0),
                unrealized_pnl=float(data.get("unrealized_pnl") or 0.0),
                gross_exposure=float(data.get("gross_exposure") or 0.0),
                as_of=as_of,
                session_date=session_date,
            )
        except Exception:
            return None

    def get_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> Optional[PnLSnapshot]:
        raw = self._redis.get(self._key(tenant_id, broker_account_id, strategy_id))
        return self._deserialize(raw)

    def upsert_snapshot(self, snapshot: PnLSnapshot) -> None:
        self._redis.set(
            self._key(
                snapshot.key.tenant_id,
                snapshot.key.broker_account_id,
                snapshot.key.strategy_id,
            ),
            self._serialize(snapshot),
        )

    def list_account_snapshots(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> list[PnLSnapshot]:
        pattern = f"{self._prefix}:{tenant_id}:{broker_account_id}:*"
        result: list[PnLSnapshot] = []
        for key in self._redis.scan_iter(match=pattern):
            raw = self._redis.get(key)
            snap = self._deserialize(raw)
            if snap is not None:
                result.append(snap)
        return result


def _firestore_collection_name(settings: Settings) -> str:
    prefix = str(settings.firestore_collection_prefix or "").strip()
    if prefix and not prefix.endswith("_"):
        prefix = f"{prefix}_"
    return os.getenv("PNL_STATE_COLLECTION", f"{prefix}pnl_snapshots")


def build_pnl_state_store(settings: Optional[Settings] = None) -> PnLStateStore:
    settings = settings or get_settings()
    backend = str(
        os.getenv("PNL_STATE_BACKEND", settings.control_plane_backend or "firestore")
    ).strip().lower()

    if backend in {"inmemory", "memory", "local"}:
        return InMemoryPnLStateStore()

    if backend in {"postgres", "pg", "db", "database"}:
        table_name = os.getenv("PNL_STATE_TABLE", "pnl_snapshots")
        try:
            dsn = get_control_plane_dsn(settings)
            return PostgresPnLStateStore(dsn=dsn, table_name=table_name)
        except Exception as exc:
            logger.warning(
                "PnL state backend postgres unavailable; falling back to in-memory: %s",
                exc,
            )
            return InMemoryPnLStateStore()

    if backend in {"redis", "cache"}:
        redis_url = str(os.getenv("PNL_STATE_REDIS_URL", "")).strip()
        if not redis_url:
            logger.warning(
                "PnL state backend redis requested but PNL_STATE_REDIS_URL is missing; falling back to in-memory"
            )
            return InMemoryPnLStateStore()
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(redis_url, decode_responses=False)
            client.ping()
            return RedisPnLStateStore(
                redis_client=client,
                key_prefix=os.getenv("PNL_STATE_REDIS_PREFIX", "pnl_snapshot"),
            )
        except Exception as exc:
            logger.warning(
                "PnL state backend redis unavailable; falling back to in-memory: %s",
                exc,
            )
            return InMemoryPnLStateStore()

    try:
        from google.cloud import firestore  # type: ignore

        client = firestore.Client(project=settings.gcp_project_id or None)
        return FirestorePnLStateStore(
            client=client,
            collection_name=_firestore_collection_name(settings),
        )
    except Exception as exc:
        logger.warning(
            "PnL state backend firestore unavailable; falling back to in-memory: %s",
            exc,
        )
        return InMemoryPnLStateStore()


__all__ = [
    "PnLStateStore",
    "InMemoryPnLStateStore",
    "FirestorePnLStateStore",
    "PostgresPnLStateStore",
    "RedisPnLStateStore",
    "build_pnl_state_store",
]
