"""
Per-OwnershipKey mutation serializer (Architecture §19.2).

All authoritative mutations for the same OwnershipKey are serialized through
a single-writer mechanism so that concurrent callers (strategy eval, lifecycle
poller, reconciliation, admin break-glass) never race on the same scope.

Different OwnershipKeys proceed fully in parallel -- only same-key work is
serialized.  When multiple mutations contend for the same scope, the one with
the highest priority (lowest numeric value) executes first.

Priority ordering (§19.2):
  1  BREAK_GLASS       risk-reducing break-glass / forced-flatten
  2  TERMINAL_EVIDENCE  terminal lifecycle / fill evidence
  3  RECONCILIATION     reconciliation decisions
  4  ROUTINE_EXIT       routine strategy exits
  5  FRESH_ENTRY        fresh strategy entries
"""

from __future__ import annotations

import enum
import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mutation priority
# ---------------------------------------------------------------------------

class MutationPriority(int, enum.Enum):
    """Priority tiers for scope-level mutation ordering.

    Lower numeric value == higher priority.
    """

    BREAK_GLASS = 1
    TERMINAL_EVIDENCE = 2
    RECONCILIATION = 3
    ROUTINE_EXIT = 4
    FRESH_ENTRY = 5


# ---------------------------------------------------------------------------
# Scoped mutation descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopedMutation:
    """A single authoritative mutation targeting one OwnershipKey."""

    ownership_key: str
    priority: MutationPriority
    mutation_fn: Callable[[], Any]
    request_id: str
    actor: str  # e.g. "lifecycle_poller", "strategy_eval", "reconciliation", "admin_breakglass"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # -- helpers for heap ordering ------------------------------------------

    def _sort_key(self) -> tuple:
        """(priority_value, created_at_timestamp) -- lowest wins."""
        return (self.priority.value, self.created_at.timestamp())


# ---------------------------------------------------------------------------
# Internal per-scope state
# ---------------------------------------------------------------------------

class _ScopeState:
    """Manages a lock and a priority queue for a single OwnershipKey."""

    __slots__ = ("lock", "queue", "queue_lock", "last_activity")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.queue: List[tuple] = []          # min-heap of (sort_key, sequence, mutation)
        self.queue_lock = threading.Lock()     # protects the heap itself
        self.last_activity: float = time.monotonic()

    def enqueue(self, mutation: ScopedMutation, seq: int) -> None:
        with self.queue_lock:
            heapq.heappush(self.queue, (mutation._sort_key(), seq, mutation))

    def dequeue(self) -> Optional[ScopedMutation]:
        with self.queue_lock:
            if self.queue:
                _, _, mutation = heapq.heappop(self.queue)
                return mutation
            return None

    def pending(self) -> int:
        with self.queue_lock:
            return len(self.queue)

    def touch(self) -> None:
        self.last_activity = time.monotonic()


# ---------------------------------------------------------------------------
# Scope mutation serializer
# ---------------------------------------------------------------------------

class ScopeMutationSerializer:
    """Thread-safe per-OwnershipKey mutation serializer.

    Concurrent mutations targeting *different* OwnershipKeys proceed in
    parallel.  Mutations targeting the *same* key are serialized; when
    multiple are waiting, the highest-priority (lowest enum value) executes
    first.

    Parameters
    ----------
    max_scope_locks:
        Upper bound on tracked scopes.  When exceeded, the least-recently-
        used idle scopes are evicted.
    stale_seconds:
        Scopes with no activity for this many seconds *and* no pending work
        are eligible for cleanup.
    """

    def __init__(
        self,
        *,
        max_scope_locks: int = 10_000,
        stale_seconds: float = 300.0,
    ) -> None:
        self._max_scope_locks = max_scope_locks
        self._stale_seconds = stale_seconds

        self._scopes: Dict[str, _ScopeState] = {}
        self._scopes_lock = threading.Lock()  # protects the dict itself
        self._sequence = 0                     # monotonic tiebreaker
        self._seq_lock = threading.Lock()
        self._active_scope_local = threading.local()

    # -- public API ---------------------------------------------------------

    def execute(self, mutation: ScopedMutation) -> Any:
        """Acquire the per-scope lock, execute *mutation*, return result.

        If the scope lock is already held by another thread, this call blocks
        until its turn.  Pending mutations are dispatched in priority order.

        Raises whatever *mutation.mutation_fn* raises; the scope lock is
        always released.
        """
        if self._is_scope_active_on_current_thread(mutation.ownership_key):
            scope = self._get_scope_state(mutation.ownership_key)
            scope.touch()
            return mutation.mutation_fn()

        scope = self._get_scope_state(mutation.ownership_key)
        seq = self._next_seq()
        scope.enqueue(mutation, seq)

        scope.lock.acquire()
        try:
            # Drain in priority order -- our mutation is guaranteed to be in
            # the queue, but higher-priority work submitted while we waited
            # should run first.  We execute everything up to and including
            # our own entry.
            result: Any = None
            executed_ours = False
            while not executed_ours:
                next_mutation = scope.dequeue()
                if next_mutation is None:
                    # Should not happen, but be defensive.
                    break
                is_ours = (
                    next_mutation.request_id == mutation.request_id
                    and next_mutation.actor == mutation.actor
                )
                try:
                    self._push_active_scope(next_mutation.ownership_key)
                    try:
                        fn_result = next_mutation.mutation_fn()
                    finally:
                        self._pop_active_scope(next_mutation.ownership_key)
                except Exception:
                    logger.exception(
                        "scope_serializer: mutation failed  "
                        "scope=%s  actor=%s  request_id=%s  priority=%s",
                        next_mutation.ownership_key,
                        next_mutation.actor,
                        next_mutation.request_id,
                        next_mutation.priority.name,
                    )
                    if is_ours:
                        raise
                    # Non-ours failure: log and keep draining.
                    continue
                else:
                    if is_ours:
                        result = fn_result
                        executed_ours = True
            scope.touch()
            return result
        finally:
            scope.lock.release()

    def pending_count(self, ownership_key: str) -> int:
        """Return the number of mutations queued for *ownership_key*."""
        with self._scopes_lock:
            scope = self._scopes.get(ownership_key)
        if scope is None:
            return 0
        return scope.pending()

    def active_scopes(self) -> list[str]:
        """Return OwnershipKeys that currently have a scope state entry."""
        with self._scopes_lock:
            return list(self._scopes.keys())

    # -- internal -----------------------------------------------------------

    def _get_scope_state(self, ownership_key: str) -> _ScopeState:
        """Return (or create) the _ScopeState for *ownership_key*."""
        with self._scopes_lock:
            scope = self._scopes.get(ownership_key)
            if scope is not None:
                return scope

            # Evict stale entries before inserting.
            if len(self._scopes) >= self._max_scope_locks:
                self._cleanup_stale_locks()

            # If still at capacity after cleanup, evict the oldest idle scope.
            if len(self._scopes) >= self._max_scope_locks:
                self._evict_oldest_idle()

            scope = _ScopeState()
            self._scopes[ownership_key] = scope
            return scope

    def _cleanup_stale_locks(self) -> None:
        """Remove scopes with no pending work and no recent activity.

        Must be called while *self._scopes_lock* is held.
        """
        now = time.monotonic()
        stale_keys = [
            key
            for key, state in self._scopes.items()
            if state.pending() == 0
            and (now - state.last_activity) > self._stale_seconds
            and not state.lock.locked()
        ]
        for key in stale_keys:
            del self._scopes[key]
        if stale_keys:
            logger.debug(
                "scope_serializer: cleaned %d stale scope locks", len(stale_keys)
            )

    def _evict_oldest_idle(self) -> None:
        """Evict the single oldest idle scope to make room.

        Must be called while *self._scopes_lock* is held.
        """
        oldest_key: Optional[str] = None
        oldest_time = float("inf")
        for key, state in self._scopes.items():
            if state.pending() == 0 and not state.lock.locked():
                if state.last_activity < oldest_time:
                    oldest_time = state.last_activity
                    oldest_key = key
        if oldest_key is not None:
            del self._scopes[oldest_key]
            logger.debug(
                "scope_serializer: evicted idle scope %s (LRU)", oldest_key
            )

    def _next_seq(self) -> int:
        """Return a monotonically increasing sequence number for heap tiebreaking."""
        with self._seq_lock:
            self._sequence += 1
            return self._sequence

    def _active_scopes_for_current_thread(self) -> set[str]:
        scopes = getattr(self._active_scope_local, "scopes", None)
        if scopes is None:
            scopes = set()
            self._active_scope_local.scopes = scopes
        return scopes

    def _is_scope_active_on_current_thread(self, ownership_key: str) -> bool:
        return ownership_key in self._active_scopes_for_current_thread()

    def _push_active_scope(self, ownership_key: str) -> None:
        self._active_scopes_for_current_thread().add(ownership_key)

    def _pop_active_scope(self, ownership_key: str) -> None:
        self._active_scopes_for_current_thread().discard(ownership_key)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

scope_serializer = ScopeMutationSerializer()
