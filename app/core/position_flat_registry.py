"""
PositionFlatRegistry — decoupled subscription registry for broker-flat notifications.

Strategies subscribe once at startup. When position_sync confirms a STRONG-evidence
flat for a scope label, it notifies all subscribers via notify(). This replaces the
fragile hub_runtime.routing_table path that silently becomes a no-op if the routing
table API changes.

Thread-safe; designed for write-once-at-startup / read-many-at-runtime access.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

# Callback type: (label: str, reason: str) -> None
FlatCallback = Callable[[str, str], None]


class PositionFlatRegistry:
    """Thread-safe registry of on_position_flat_by_sync callbacks.

    Usage:
        # At strategy init:
        position_flat_registry.subscribe(my_strategy)

        # From position_sync after STRONG-evidence flat:
        position_flat_registry.notify(label="NIFTY_ATM_CE_24300", reason="pos_sync_strong_flat")
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[Any] = []  # objects with on_position_flat_by_sync method

    def subscribe(self, strategy: Any) -> None:
        """Register a strategy to receive flat notifications.

        The strategy must expose on_position_flat_by_sync(*, label, reason).
        Duplicate registrations are silently ignored.
        """
        if not callable(getattr(strategy, "on_position_flat_by_sync", None)):
            logger.warning(
                "PositionFlatRegistry.subscribe: %r has no on_position_flat_by_sync method",
                strategy,
            )
            return
        with self._lock:
            if strategy not in self._subscribers:
                self._subscribers.append(strategy)
                logger.debug(
                    "PositionFlatRegistry: registered %r (total=%d)",
                    strategy, len(self._subscribers),
                )

    def unsubscribe(self, strategy: Any) -> None:
        """Deregister a strategy. Safe to call even if not registered."""
        with self._lock:
            try:
                self._subscribers.remove(strategy)
            except ValueError:
                pass

    def notify(self, label: str, reason: str) -> None:
        """Notify all subscribers that a scope label is authoritatively flat.

        Exceptions raised by individual callbacks are caught and logged so that
        one misbehaving strategy cannot block notification of the others.
        """
        with self._lock:
            subscribers = list(self._subscribers)

        for strategy in subscribers:
            fn = getattr(strategy, "on_position_flat_by_sync", None)
            if not callable(fn):
                continue
            try:
                fn(label=label, reason=reason)
            except Exception as exc:
                logger.warning(
                    "PositionFlatRegistry.notify: callback failed for %r label=%s: %s",
                    strategy, label, exc,
                )

    def clear(self) -> None:
        """Remove all subscribers. Used in tests."""
        with self._lock:
            self._subscribers.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._subscribers)


# Module-level singleton — imported by position_sync and strategies.
position_flat_registry = PositionFlatRegistry()

__all__ = ["PositionFlatRegistry", "position_flat_registry"]
