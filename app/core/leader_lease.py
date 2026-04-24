"""
Leader lease to ensure a single active trading worker.
Supports Firestore and Postgres-backed exclusivity.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

try:  # pragma: no cover - optional dependency in local/dev
    from google.cloud import firestore
except Exception:  # pragma: no cover - optional dependency in local/dev
    firestore = None

from app.data.postgres import connect_with_retry, get_control_plane_dsn

logger = logging.getLogger(__name__)


class LeaderLease:
    """
    Firestore-backed leader lease used to ensure only one instance
    runs trading workers at a time.
    """

    # Initialize lease settings and Firestore client state.
    def __init__(
        self,
        *,
        lease_id: str,
        ttl_seconds: int = 90,
        renew_seconds: int = 30,
        collection: str = "leader_leases",
        enabled: bool = True,
        owner_id: Optional[str] = None,
        backend: str = "firestore",
        postgres_dsn: Optional[str] = None,
    ) -> None:
        self.enabled = enabled
        self._lease_id = str(lease_id)
        self._ttl = max(10, int(ttl_seconds))
        self._renew = max(5, int(renew_seconds))
        self._collection = str(collection)
        self._owner_id = owner_id or os.getenv("HOSTNAME") or "unknown"
        self._backend = str(backend or "firestore").strip().lower() or "firestore"
        self._postgres_dsn = str(postgres_dsn or "").strip() or None
        self._client = None
        self._pg_conn = None
        self._task: Optional[asyncio.Task[None]] = None
        self._owned = False
        self._renew_failures = 0
        self._last_acquired_at: Optional[datetime] = None
        self._last_renewed_at: Optional[datetime] = None
        self._last_failure_at: Optional[datetime] = None
        self._lock_key_a, self._lock_key_b = self._postgres_lock_keys(self._lease_id)

    # Attempt to acquire the lease and start the renew loop.
    async def start(self) -> bool:
        if not self.enabled:
            logger.info(
                "leader_lease.disabled: lease=%s — running as sole writer without fencing",
                self._lease_id,
            )
            return True
        acquired = await asyncio.to_thread(self._try_acquire_sync)
        if acquired:
            self._owned = True
            now = datetime.now(timezone.utc)
            self._last_acquired_at = now
            self._last_renewed_at = now
            self._task = asyncio.create_task(
                self._renew_loop(), name=f"leader-lease-{self._lease_id}"
            )
            logger.info(
                "leader_lease.acquired: lease=%s owner=%s ttl_seconds=%d renew_seconds=%d",
                self._lease_id,
                self._owner_id,
                int(self._ttl),
                int(self._renew),
            )
        else:
            logger.info(
                "leader_lease.standby: lease=%s owner=%s — another writer holds the lease; "
                "this instance will not accept live order flow",
                self._lease_id,
                self._owner_id,
            )
        return acquired

    # Stop the renew loop and release the lease.
    async def stop(self) -> None:
        if not self.enabled:
            return
        self._owned = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None or self._pg_conn is not None:
            await asyncio.to_thread(self._release_sync)

    @staticmethod
    def _postgres_lock_keys(lease_id: str) -> tuple[int, int]:
        digest = hashlib.blake2s(str(lease_id).encode("utf-8"), digest_size=8).digest()
        return (
            int.from_bytes(digest[:4], byteorder="big", signed=True),
            int.from_bytes(digest[4:], byteorder="big", signed=True),
        )

    # Try to acquire the lease within a Firestore transaction.
    def _try_acquire_sync(self) -> bool:
        if self._backend == "postgres":
            return self._try_acquire_postgres_sync()
        return self._try_acquire_firestore_sync()

    def _try_acquire_firestore_sync(self) -> bool:
        if self._client is None:
            if firestore is None:
                logger.warning("LeaderLease disabled: Firestore client unavailable")
                return False
            self._client = firestore.Client()
        doc_ref = self._client.collection(self._collection).document(self._lease_id)
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self._ttl)
        owner = self._owner_id

        @firestore.transactional
        def txn(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else {}
            current_until = data.get("lease_until")
            current_owner = data.get("owner")
            if current_until is not None and getattr(current_until, "tzinfo", None) is None:
                current_until = current_until.replace(tzinfo=timezone.utc)
            if not current_until or current_until <= now or current_owner == owner:
                transaction.set(
                    doc_ref,
                    {
                        "owner": owner,
                        "lease_until": lease_until,
                        "updated_at": now,
                    },
                    merge=True,
                )
                return True
            return False

        transaction = self._client.transaction()
        try:
            return bool(txn(transaction))
        except Exception as exc:
            logger.warning("LeaderLease acquire failed: %s", exc)
            return False

    def _try_acquire_postgres_sync(self) -> bool:
        if self._pg_conn is not None:
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
            except Exception:
                try:
                    self._pg_conn.close()
                except Exception:
                    pass
                self._pg_conn = None
        dsn = self._postgres_dsn or get_control_plane_dsn()
        conn = connect_with_retry(dsn, autocommit=True)
        acquired = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    (self._lock_key_a, self._lock_key_b),
                )
                row = cur.fetchone()
            acquired = bool(row and row[0])
            if acquired:
                self._pg_conn = conn
                return True
            return False
        except Exception as exc:
            logger.warning("LeaderLease acquire failed: %s", exc)
            return False
        finally:
            if not acquired:
                try:
                    conn.close()
                except Exception:
                    pass

    # Renew the lease expiry if still owned.
    def _renew_sync(self) -> bool:
        if self._backend == "postgres":
            return self._renew_postgres_sync()
        return self._renew_firestore_sync()

    def _renew_firestore_sync(self) -> bool:
        if self._client is None:
            return False
        doc_ref = self._client.collection(self._collection).document(self._lease_id)
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self._ttl)
        owner = self._owner_id

        @firestore.transactional
        def txn(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("owner") != owner:
                return False
            transaction.set(
                doc_ref,
                {"lease_until": lease_until, "updated_at": now},
                merge=True,
            )
            return True

        transaction = self._client.transaction()
        try:
            return bool(txn(transaction))
        except Exception as exc:
            logger.warning("LeaderLease renew failed: %s", exc)
            return False

    def _renew_postgres_sync(self) -> bool:
        conn = self._pg_conn
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as exc:
            logger.warning("LeaderLease renew failed: %s", exc)
            return False

    # Release the lease by setting its expiry to now.
    def _release_sync(self) -> None:
        if self._backend == "postgres":
            self._release_postgres_sync()
            return
        self._release_firestore_sync()

    def _release_firestore_sync(self) -> None:
        if self._client is None:
            return
        doc_ref = self._client.collection(self._collection).document(self._lease_id)
        now = datetime.now(timezone.utc)
        try:
            doc_ref.set(
                {"owner": self._owner_id, "lease_until": now, "updated_at": now},
                merge=True,
            )
        except Exception as exc:
            logger.warning("LeaderLease release failed: %s", exc)

    def _release_postgres_sync(self) -> None:
        conn = self._pg_conn
        self._pg_conn = None
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    (self._lock_key_a, self._lock_key_b),
                )
                cur.fetchone()
        except Exception as exc:
            logger.warning("LeaderLease release failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Periodically renew the lease and exit if it is lost.
    async def _renew_loop(self) -> None:
        try:
            while self._owned:
                await asyncio.sleep(self._renew)
                ok = await asyncio.to_thread(self._renew_sync)
                if ok:
                    self._last_renewed_at = datetime.now(timezone.utc)
                    logger.debug(
                        "leader_lease.renewal_success: lease=%s next_renewal_in_seconds=%d",
                        self._lease_id,
                        int(self._renew),
                    )
                    continue
                if not ok:
                    self._renew_failures += 1
                    self._last_failure_at = datetime.now(timezone.utc)
                    try:
                        from app.observability.prometheus_metrics import counter_inc

                        counter_inc(
                            "phoenix_lease_renewal_failures_total",
                            help_text="Leader lease renewal failures",
                        )
                    except Exception:
                        logger.debug("LeaderLease metrics update skipped", exc_info=True)
                    logger.critical(
                        "leader_lease.lost: lease=%s owner=%s failures=%d — "
                        "fencing this writer to prevent dual-authority split-brain",
                        self._lease_id,
                        self._owner_id,
                        self._renew_failures,
                    )
                    self._owned = False
                    if os.getenv("LEADER_LEASE_EXIT_ON_LOSS", "true").lower() in {
                        "1",
                        "true",
                        "yes",
                    }:
                        logger.critical(
                            "leader_lease.fencing_exit: lease=%s — calling os._exit(2) "
                            "to guarantee fencing; restart will reconcile before accepting orders",
                            self._lease_id,
                        )
                        os._exit(2)
                    break
        except asyncio.CancelledError:
            raise

    def status_snapshot(self) -> dict[str, object]:
        task = self._task
        return {
            "enabled": bool(self.enabled),
            "backend": self._backend,
            "lease_id": self._lease_id,
            "owner_id": self._owner_id,
            "owned": bool(self._owned),
            "ttl_seconds": int(self._ttl),
            "renew_seconds": int(self._renew),
            "task_running": bool(task and not task.done()),
            "connection_open": bool(self._pg_conn and not getattr(self._pg_conn, "closed", False)),
            "task_name": task.get_name() if task else None,
            "renew_failures": int(self._renew_failures),
            "last_acquired_at": (
                self._last_acquired_at.isoformat().replace("+00:00", "Z")
                if self._last_acquired_at is not None
                else None
            ),
            "last_renewed_at": (
                self._last_renewed_at.isoformat().replace("+00:00", "Z")
                if self._last_renewed_at is not None
                else None
            ),
            "last_failure_at": (
                self._last_failure_at.isoformat().replace("+00:00", "Z")
                if self._last_failure_at is not None
                else None
            ),
        }
