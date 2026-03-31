"""
Durable processed-trade marker stores for order lifecycle idempotency.

Markers are keyed by (broker_account_id, broker_order_id) so duplicate fill
events can be suppressed across process restarts.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Protocol

from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)


class ProcessedTradeStore(Protocol):
    # Atomically claim a processed marker. Returns True only for first claim.
    def claim_processed(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        broker_order_id: str,
        hub_order_id: str,
        symbol: str,
        side: str,
        processed_at: datetime,
    ) -> bool: ...


class InMemoryProcessedTradeStore:
    # In-process fallback (non-durable), mainly for local runs.
    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def claim_processed(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        broker_order_id: str,
        hub_order_id: str,
        symbol: str,
        side: str,
        processed_at: datetime,
    ) -> bool:
        _ = (
            tenant_id,
            strategy_id,
            hub_order_id,
            symbol,
            side,
            processed_at,
        )
        key = (str(broker_account_id), str(broker_order_id))
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


def _is_true(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_identifier(raw: str, default: str) -> str:
    text = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw or ""))
    text = text.strip("_")
    return text or default


class PostgresProcessedTradeStore:
    # Durable marker store using Postgres upsert semantics.
    def __init__(self, *, dsn: str, table_name: str = "trade_processed_markers") -> None:
        self._dsn = dsn
        self._table = _safe_identifier(table_name, "trade_processed_markers")
        self._ensure_schema()

    def _conn(self):
        return connect_with_retry(self._dsn, autocommit=True)

    def _ensure_schema(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                broker_account_id TEXT NOT NULL,
                broker_order_id TEXT NOT NULL,
                tenant_id TEXT,
                strategy_id TEXT,
                hub_order_id TEXT,
                symbol TEXT,
                side TEXT,
                processed_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (broker_account_id, broker_order_id)
            )
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def claim_processed(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        broker_order_id: str,
        hub_order_id: str,
        symbol: str,
        side: str,
        processed_at: datetime,
    ) -> bool:
        sql = f"""
            INSERT INTO {self._table} (
                broker_account_id,
                broker_order_id,
                tenant_id,
                strategy_id,
                hub_order_id,
                symbol,
                side,
                processed_at
            ) VALUES (
                %(broker_account_id)s,
                %(broker_order_id)s,
                %(tenant_id)s,
                %(strategy_id)s,
                %(hub_order_id)s,
                %(symbol)s,
                %(side)s,
                %(processed_at)s
            )
            ON CONFLICT (broker_account_id, broker_order_id) DO NOTHING
            RETURNING broker_order_id
        """
        payload = {
            "broker_account_id": str(broker_account_id),
            "broker_order_id": str(broker_order_id),
            "tenant_id": str(tenant_id),
            "strategy_id": str(strategy_id),
            "hub_order_id": str(hub_order_id),
            "symbol": str(symbol or ""),
            "side": str(side or ""),
            "processed_at": processed_at.astimezone(timezone.utc),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
                return cur.fetchone() is not None


class FirestoreProcessedTradeStore:
    # Durable marker store using Firestore document create semantics.
    def __init__(self, *, client, collection_name: str) -> None:
        self._collection = client.collection(collection_name)

    @staticmethod
    def _doc_id(broker_account_id: BrokerAccountId, broker_order_id: str) -> str:
        acc = str(broker_account_id).replace("/", "_")
        oid = str(broker_order_id).replace("/", "_")
        return f"{acc}::{oid}"

    def claim_processed(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        broker_order_id: str,
        hub_order_id: str,
        symbol: str,
        side: str,
        processed_at: datetime,
    ) -> bool:
        payload = {
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "broker_order_id": str(broker_order_id),
            "strategy_id": str(strategy_id),
            "hub_order_id": str(hub_order_id),
            "symbol": str(symbol or ""),
            "side": str(side or ""),
            "processed_at": processed_at.astimezone(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        doc_ref = self._collection.document(
            self._doc_id(broker_account_id, broker_order_id)
        )
        try:
            doc_ref.create(payload)
            return True
        except Exception as exc:
            text = f"{exc.__class__.__name__}:{exc}"
            if "AlreadyExists" in text or "409" in text:
                return False
            raise


def build_processed_trade_store() -> ProcessedTradeStore:
    # Build durable store from configured control-plane backend.
    settings = get_settings()
    strict_required = _is_true(
        os.getenv(
            "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED",
            str(getattr(settings, "order_lifecycle_persist_markers_required", False)),
        )
    )
    persist_enabled = not (
        str(os.getenv("ORDER_LIFECYCLE_PERSIST_MARKERS", "true")).strip().lower()
        in {
            "0",
            "false",
            "no",
        }
    )
    if not persist_enabled:
        if strict_required:
            raise RuntimeError(
                "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED=true requires durable markers; "
                "ORDER_LIFECYCLE_PERSIST_MARKERS is disabled"
            )
        return InMemoryProcessedTradeStore()

    backend = str(
        os.getenv("ORDER_LIFECYCLE_PROCESSED_BACKEND", settings.control_plane_backend)
    ).strip().lower()

    if backend in {"postgres", "pg", "db", "database"}:
        table_name = os.getenv(
            "ORDER_LIFECYCLE_PROCESSED_TABLE",
            "trade_processed_markers",
        )
        try:
            dsn = get_control_plane_dsn(settings)
            return PostgresProcessedTradeStore(dsn=dsn, table_name=table_name)
        except Exception as exc:
            if strict_required:
                logger.error(
                    "Order lifecycle durable marker init failed backend=postgres strict=true error=%s",
                    exc,
                )
                raise RuntimeError(
                    f"Order lifecycle durable marker backend initialization failed "
                    f"(backend=postgres): {exc}"
                ) from exc
            logger.warning(
                "Order lifecycle durable marker falling back to in-memory (postgres init failed): %s",
                exc,
            )
            return InMemoryProcessedTradeStore()

    prefix = str(settings.firestore_collection_prefix or "").strip()
    if prefix and not prefix.endswith("_"):
        prefix = f"{prefix}_"
    collection_name = os.getenv(
        "ORDER_LIFECYCLE_PROCESSED_COLLECTION",
        f"{prefix}trade_processed_markers",
    )
    try:
        from google.cloud import firestore  # type: ignore

        client = firestore.Client(project=settings.gcp_project_id or None)
        return FirestoreProcessedTradeStore(
            client=client,
            collection_name=collection_name,
        )
    except Exception as exc:
        if strict_required:
            logger.error(
                "Order lifecycle durable marker init failed backend=firestore strict=true error=%s",
                exc,
            )
            raise RuntimeError(
                f"Order lifecycle durable marker backend initialization failed "
                f"(backend=firestore): {exc}"
            ) from exc
        logger.warning(
            "Order lifecycle durable marker falling back to in-memory (firestore init failed): %s",
            exc,
        )
        return InMemoryProcessedTradeStore()


__all__ = [
    "ProcessedTradeStore",
    "InMemoryProcessedTradeStore",
    "PostgresProcessedTradeStore",
    "FirestoreProcessedTradeStore",
    "build_processed_trade_store",
]

