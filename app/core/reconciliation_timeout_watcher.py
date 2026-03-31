"""Reconciliation timeout watcher with ORPHAN_REVIEW escalation (Architecture S11.5-11.6).

Monitors scopes stuck in RECONCILING state beyond a configurable threshold
and auto-escalates to ORPHAN_REVIEW.  Freezes fresh entries for affected
OwnershipKeys until resolved.

When RECONCILING or UNKNOWN exceeds policy threshold, Phoenix must freeze
fresh entries and emit alerts until convergence.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReconcilingScope:
    """Tracks a scope that entered RECONCILING state."""
    scope_key: str
    entered_at: float  # monotonic time
    wall_entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    escalated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ReconciliationTimeoutWatcher:
    """Background watcher that escalates stale RECONCILING scopes.

    Parameters
    ----------
    timeout_seconds:
        How long a scope can remain in RECONCILING before escalation.
    check_interval_seconds:
        How often the watcher checks for stale scopes.
    on_escalate:
        Callback invoked when a scope is escalated to ORPHAN_REVIEW.
        Signature: (scope_key: str, age_seconds: float, metadata: dict) -> None
    on_entry_freeze:
        Callback invoked to freeze entries for an OwnershipKey.
        Signature: (scope_key: str) -> None
    """

    def __init__(
        self,
        *,
        timeout_seconds: Optional[float] = None,
        check_interval_seconds: float = 30.0,
        on_escalate: Optional[Callable[[str, float, dict], None]] = None,
        on_entry_freeze: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._timeout_seconds = float(
            timeout_seconds
            or os.environ.get("RECONCILIATION_TIMEOUT_SECONDS", "300")
        )
        self._check_interval = max(5.0, check_interval_seconds)
        self._on_escalate = on_escalate
        self._on_entry_freeze = on_entry_freeze

        self._scopes: Dict[str, ReconcilingScope] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._now_mono = time.monotonic

    # -- Public API ---------------------------------------------------------

    def mark_reconciling(
        self,
        scope_key: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Register that a scope has entered RECONCILING."""
        with self._lock:
            if scope_key not in self._scopes:
                self._scopes[scope_key] = ReconcilingScope(
                    scope_key=scope_key,
                    entered_at=self._now_mono(),
                    metadata=dict(metadata or {}),
                )
                logger.info(
                    "reconciliation_watcher: scope %s entered RECONCILING", scope_key
                )

    def mark_resolved(self, scope_key: str) -> None:
        """Remove a scope that has been resolved (no longer RECONCILING)."""
        with self._lock:
            removed = self._scopes.pop(scope_key, None)
            if removed:
                age = self._now_mono() - removed.entered_at
                logger.info(
                    "reconciliation_watcher: scope %s resolved after %.1fs",
                    scope_key, age,
                )

    def is_stale(self, scope_key: str) -> bool:
        """Check if a scope has exceeded the timeout threshold."""
        with self._lock:
            scope = self._scopes.get(scope_key)
            if scope is None:
                return False
            return (self._now_mono() - scope.entered_at) > self._timeout_seconds

    def active_scopes(self) -> list[ReconcilingScope]:
        with self._lock:
            return list(self._scopes.values())

    def stale_count(self) -> int:
        now = self._now_mono()
        with self._lock:
            return sum(
                1 for s in self._scopes.values()
                if (now - s.entered_at) > self._timeout_seconds
            )

    # -- Background watcher -------------------------------------------------

    def start(self) -> None:
        """Start the background watcher thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="reconciliation-timeout-watcher", daemon=True,
        )
        self._thread.start()
        logger.info(
            "reconciliation_timeout_watcher started (timeout=%.0fs, interval=%.0fs)",
            self._timeout_seconds, self._check_interval,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self._check_interval):
            self._check_timeouts()

    def _check_timeouts(self) -> None:
        now = self._now_mono()
        to_escalate: list[tuple[str, float, dict]] = []

        with self._lock:
            for scope_key, scope in self._scopes.items():
                age = now - scope.entered_at
                if age > self._timeout_seconds and not scope.escalated:
                    scope.escalated = True
                    to_escalate.append((scope_key, age, dict(scope.metadata)))

        for scope_key, age, metadata in to_escalate:
            logger.warning(
                "reconciliation_timeout_watcher: escalating scope %s to ORPHAN_REVIEW "
                "(stuck %.1fs > %.1fs threshold)",
                scope_key, age, self._timeout_seconds,
            )
            if self._on_entry_freeze:
                try:
                    self._on_entry_freeze(scope_key)
                except Exception:
                    logger.exception(
                        "reconciliation_watcher: entry freeze callback failed for %s",
                        scope_key,
                    )
            if self._on_escalate:
                try:
                    self._on_escalate(scope_key, age, metadata)
                except Exception:
                    logger.exception(
                        "reconciliation_watcher: escalation callback failed for %s",
                        scope_key,
                    )


# Module-level singleton
reconciliation_timeout_watcher = ReconciliationTimeoutWatcher()

__all__ = [
    "ReconciliationTimeoutWatcher",
    "ReconcilingScope",
    "reconciliation_timeout_watcher",
]
