"""
Firestore-backed leader lease to ensure a single active trading worker.
Handles acquire, renew, and release with async helpers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

try:  # pragma: no cover - optional dependency in local/dev
    from google.cloud import firestore
except Exception:  # pragma: no cover - optional dependency in local/dev
    firestore = None

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
    ) -> None:
        self.enabled = enabled
        self._lease_id = str(lease_id)
        self._ttl = max(10, int(ttl_seconds))
        self._renew = max(5, int(renew_seconds))
        self._collection = str(collection)
        self._owner_id = owner_id or os.getenv("HOSTNAME") or "unknown"
        self._client = None
        self._task: Optional[asyncio.Task[None]] = None
        self._owned = False
        self._renew_failures = 0
        self._last_acquired_at: Optional[datetime] = None
        self._last_renewed_at: Optional[datetime] = None
        self._last_failure_at: Optional[datetime] = None

    # Attempt to acquire the lease and start the renew loop.
    async def start(self) -> bool:
        if not self.enabled:
            return True
        if firestore is None:
            logger.warning("LeaderLease disabled: Firestore client unavailable")
            return False
        if self._client is None:
            self._client = firestore.Client()
        acquired = await asyncio.to_thread(self._try_acquire_sync)
        if acquired:
            self._owned = True
            now = datetime.now(timezone.utc)
            self._last_acquired_at = now
            self._last_renewed_at = now
            self._task = asyncio.create_task(
                self._renew_loop(), name=f"leader-lease-{self._lease_id}"
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
        if self._client:
            await asyncio.to_thread(self._release_sync)

    # Try to acquire the lease within a Firestore transaction.
    def _try_acquire_sync(self) -> bool:
        if self._client is None:
            return False
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

    # Renew the lease expiry if still owned.
    def _renew_sync(self) -> bool:
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

    # Release the lease by setting its expiry to now.
    def _release_sync(self) -> None:
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

    # Periodically renew the lease and exit if it is lost.
    async def _renew_loop(self) -> None:
        try:
            while self._owned:
                await asyncio.sleep(self._renew)
                ok = await asyncio.to_thread(self._renew_sync)
                if ok:
                    self._last_renewed_at = datetime.now(timezone.utc)
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
                    logger.error("Leader lease lost for %s", self._lease_id)
                    self._owned = False
                    if os.getenv("LEADER_LEASE_EXIT_ON_LOSS", "true").lower() in {
                        "1",
                        "true",
                        "yes",
                    }:
                        os._exit(2)
                    break
        except asyncio.CancelledError:
            raise

    def status_snapshot(self) -> dict[str, object]:
        task = self._task
        return {
            "enabled": bool(self.enabled),
            "lease_id": self._lease_id,
            "owner_id": self._owner_id,
            "owned": bool(self._owned),
            "ttl_seconds": int(self._ttl),
            "renew_seconds": int(self._renew),
            "task_running": bool(task and not task.done()),
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
