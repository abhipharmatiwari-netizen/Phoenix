"""
Order lifecycle state machine -- Architecture S3.2.

Defines the canonical 16-state order lifecycle, valid transitions,
and bridge mappings to the durable outbox status model.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, FrozenSet, Optional

from app.orders.order_outbox import (
    OUTBOX_STATUS_ACKED,
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_SUBMITTING,
    OUTBOX_STATUS_TERMINAL_FILL,
    OUTBOX_STATUS_TERMINAL_NON_FILL,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. OrderLifecycleState enum
# ---------------------------------------------------------------------------


class OrderLifecycleState(str, Enum):
    """Canonical 16-state order lifecycle per Architecture S3.2."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    OPEN = "OPEN"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    RECOVERY_PENDING = "RECOVERY_PENDING"


# ---------------------------------------------------------------------------
# 2-5. State classification sets
# ---------------------------------------------------------------------------

TERMINAL_ORDER_STATES: FrozenSet[OrderLifecycleState] = frozenset(
    {
        OrderLifecycleState.FILLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.FAILED,
    }
)

TERMINAL_FILL_STATES: FrozenSet[OrderLifecycleState] = frozenset(
    {
        OrderLifecycleState.FILLED,
    }
)

TERMINAL_NON_FILL_STATES: FrozenSet[OrderLifecycleState] = frozenset(
    {
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.FAILED,
    }
)

ACTIVE_ORDER_STATES: FrozenSet[OrderLifecycleState] = frozenset(
    s for s in OrderLifecycleState if s not in TERMINAL_ORDER_STATES
)


# ---------------------------------------------------------------------------
# 6. Valid transitions
# ---------------------------------------------------------------------------

VALID_ORDER_TRANSITIONS: Dict[OrderLifecycleState, FrozenSet[OrderLifecycleState]] = {
    OrderLifecycleState.CREATED: frozenset(
        {
            OrderLifecycleState.VALIDATED,
            OrderLifecycleState.FAILED,
        }
    ),
    OrderLifecycleState.VALIDATED: frozenset(
        {
            OrderLifecycleState.SUBMITTING,
            OrderLifecycleState.FAILED,
        }
    ),
    OrderLifecycleState.SUBMITTING: frozenset(
        {
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.ACKED,
            OrderLifecycleState.OPEN,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.TIMEOUT,
        }
    ),
    OrderLifecycleState.SUBMITTED: frozenset(
        {
            OrderLifecycleState.ACKED,
            OrderLifecycleState.OPEN,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.TIMEOUT,
            OrderLifecycleState.UNKNOWN,
        }
    ),
    OrderLifecycleState.ACKED: frozenset(
        {
            OrderLifecycleState.OPEN,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
        }
    ),
    OrderLifecycleState.OPEN: frozenset(
        {
            OrderLifecycleState.PARTIAL_FILL,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCEL_REQUESTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
            OrderLifecycleState.RECONCILING,
        }
    ),
    OrderLifecycleState.PARTIAL_FILL: frozenset(
        {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCEL_REQUESTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
            OrderLifecycleState.RECONCILING,
        }
    ),
    OrderLifecycleState.CANCEL_REQUESTED: frozenset(
        {
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
            OrderLifecycleState.RECONCILING,
        }
    ),
    OrderLifecycleState.TIMEOUT: frozenset(
        {
            OrderLifecycleState.RECONCILING,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
        }
    ),
    OrderLifecycleState.UNKNOWN: frozenset(
        {
            OrderLifecycleState.RECONCILING,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
        }
    ),
    OrderLifecycleState.RECONCILING: frozenset(
        {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
        }
    ),
    OrderLifecycleState.RECOVERY_PENDING: frozenset(
        {
            OrderLifecycleState.CREATED,
            OrderLifecycleState.SUBMITTING,
            OrderLifecycleState.OPEN,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.RECONCILING,
            OrderLifecycleState.UNKNOWN,
        }
    ),
    # Terminal states have no outbound transitions.
    OrderLifecycleState.FILLED: frozenset(),
    OrderLifecycleState.REJECTED: frozenset(),
    OrderLifecycleState.CANCELLED: frozenset(),
    OrderLifecycleState.EXPIRED: frozenset(),
    OrderLifecycleState.FAILED: frozenset(),
}


# ---------------------------------------------------------------------------
# 7. Transition validator
# ---------------------------------------------------------------------------


class InvalidOrderTransition(Exception):
    """Raised when an order transition violates the lifecycle state machine."""

    def __init__(
        self,
        current: OrderLifecycleState,
        target: OrderLifecycleState,
        message: Optional[str] = None,
    ) -> None:
        self.current = current
        self.target = target
        detail = message or (
            f"Invalid order transition: {current.value} -> {target.value}"
        )
        super().__init__(detail)


def validate_order_transition(
    current: OrderLifecycleState,
    target: OrderLifecycleState,
) -> None:
    """Raise ``InvalidOrderTransition`` if *current* -> *target* is not allowed.

    A no-op self-transition (current == target) is always permitted.
    """
    if current == target:
        return
    allowed = VALID_ORDER_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise InvalidOrderTransition(current, target)


# ---------------------------------------------------------------------------
# 8-9. Helpers
# ---------------------------------------------------------------------------


def is_terminal(state: OrderLifecycleState) -> bool:
    """Return ``True`` when *state* is a terminal lifecycle state."""
    return state in TERMINAL_ORDER_STATES


def is_terminal_fill(state: OrderLifecycleState) -> bool:
    """Return ``True`` when *state* represents a successful fill completion."""
    return state in TERMINAL_FILL_STATES


# ---------------------------------------------------------------------------
# 10. Broker status classifier
# ---------------------------------------------------------------------------

# Normalised broker status string -> OrderLifecycleState.
# Keys are upper-cased for case-insensitive lookup.
_BROKER_STATUS_MAP: Dict[str, OrderLifecycleState] = {
    # Filled / complete
    "COMPLETE": OrderLifecycleState.FILLED,
    "COMPLETED": OrderLifecycleState.FILLED,
    "FILLED": OrderLifecycleState.FILLED,
    "FULL": OrderLifecycleState.FILLED,
    "EXECUTED": OrderLifecycleState.FILLED,
    # Success / acknowledged -- broker placement ack (e.g. Angel "success")
    "SUCCESS": OrderLifecycleState.ACKED,
    "SUCCESSFUL": OrderLifecycleState.ACKED,
    "OK": OrderLifecycleState.ACKED,
    "ACCEPTED": OrderLifecycleState.ACKED,
    "ACK": OrderLifecycleState.ACKED,
    "ACKNOWLEDGED": OrderLifecycleState.ACKED,
    # Rejected
    "REJECTED": OrderLifecycleState.REJECTED,
    "REJECT": OrderLifecycleState.REJECTED,
    # Cancelled (various spellings)
    "CANCELLED": OrderLifecycleState.CANCELLED,
    "CANCELED": OrderLifecycleState.CANCELLED,
    "CANCEL": OrderLifecycleState.CANCELLED,
    # Expired
    "EXPIRED": OrderLifecycleState.EXPIRED,
    # Failed / error
    "FAILED": OrderLifecycleState.FAILED,
    "ERROR": OrderLifecycleState.FAILED,
    "FAILURE": OrderLifecycleState.FAILED,
    # Open / active
    "OPEN": OrderLifecycleState.OPEN,
    "OPEN PENDING": OrderLifecycleState.OPEN,
    "AFTER MARKET ORDER REQ RECEIVED": OrderLifecycleState.OPEN,
    "AMO REQ RECEIVED": OrderLifecycleState.OPEN,
    "TRIGGER PENDING": OrderLifecycleState.OPEN,
    "VALIDATION PENDING": OrderLifecycleState.SUBMITTED,
    "PUT ORDER REQ RECEIVED": OrderLifecycleState.SUBMITTED,
    # Partial
    "PARTIAL": OrderLifecycleState.PARTIAL_FILL,
    "PARTIALLY FILLED": OrderLifecycleState.PARTIAL_FILL,
    "PARTIAL FILL": OrderLifecycleState.PARTIAL_FILL,
    # Cancel requested
    "CANCEL REQUESTED": OrderLifecycleState.CANCEL_REQUESTED,
    "CANCEL PENDING": OrderLifecycleState.CANCEL_REQUESTED,
    "MODIFY PENDING": OrderLifecycleState.CANCEL_REQUESTED,
    # Pending / not-yet-acked
    "PENDING": OrderLifecycleState.SUBMITTED,
    "NOT CANCELLED": OrderLifecycleState.OPEN,
    "NOT MODIFIED": OrderLifecycleState.OPEN,
    # Boolean-as-string: some brokers return True/False as strings
    "TRUE": OrderLifecycleState.ACKED,
    "FALSE": OrderLifecycleState.FAILED,
    "1": OrderLifecycleState.ACKED,
    "0": OrderLifecycleState.FAILED,
}


def classify_broker_status(broker_status_str) -> OrderLifecycleState:
    """Map a raw broker status string to a canonical ``OrderLifecycleState``.

    Accepts str, bool, int, or None.  Performs case-insensitive,
    whitespace-trimmed lookup.  Returns ``OrderLifecycleState.UNKNOWN``
    for genuinely unrecognised values.
    """
    # ── Handle bool / int / truthy broker responses ──────────────────
    # Some brokers return True / 1 for successful placement, False / 0
    # for failure.  Normalise *before* stringifying so ``True`` does not
    # become the unhelpful string ``"TRUE"``.
    if isinstance(broker_status_str, bool):
        return OrderLifecycleState.ACKED if broker_status_str else OrderLifecycleState.FAILED
    if isinstance(broker_status_str, int):
        return OrderLifecycleState.ACKED if broker_status_str else OrderLifecycleState.FAILED
    normalised = str(broker_status_str or "").strip().upper()
    state = _BROKER_STATUS_MAP.get(normalised)
    if state is not None:
        return state
    # Fuzzy fallbacks for substring matches.
    if "COMPLETE" in normalised or ("FILL" in normalised and "PARTIAL" not in normalised):
        return OrderLifecycleState.FILLED
    if "SUCCESS" in normalised or "ACCEPTED" in normalised or normalised == "ACK":
        return OrderLifecycleState.ACKED
    if "REJECT" in normalised:
        return OrderLifecycleState.REJECTED
    if "CANCEL" in normalised:
        return OrderLifecycleState.CANCELLED
    if "EXPIR" in normalised:
        return OrderLifecycleState.EXPIRED
    if "FAIL" in normalised or "ERROR" in normalised:
        return OrderLifecycleState.FAILED
    if "PARTIAL" in normalised:
        return OrderLifecycleState.PARTIAL_FILL
    if "OPEN" in normalised or "TRIGGER" in normalised or "PENDING" in normalised:
        return OrderLifecycleState.OPEN
    logger.warning(
        "classify_broker_status: unrecognised broker status %r, mapping to UNKNOWN",
        broker_status_str,
    )
    return OrderLifecycleState.UNKNOWN


# ---------------------------------------------------------------------------
# Outbox bridge mapping
# ---------------------------------------------------------------------------

_LIFECYCLE_TO_OUTBOX: Dict[OrderLifecycleState, str] = {
    OrderLifecycleState.CREATED: OUTBOX_STATUS_PENDING,
    OrderLifecycleState.VALIDATED: OUTBOX_STATUS_PENDING,
    OrderLifecycleState.SUBMITTING: OUTBOX_STATUS_SUBMITTING,
    OrderLifecycleState.SUBMITTED: OUTBOX_STATUS_SUBMITTING,
    OrderLifecycleState.ACKED: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.OPEN: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.PARTIAL_FILL: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.CANCEL_REQUESTED: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.TIMEOUT: OUTBOX_STATUS_SUBMITTING,
    OrderLifecycleState.UNKNOWN: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.RECONCILING: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.RECOVERY_PENDING: OUTBOX_STATUS_ACKED,
    OrderLifecycleState.FILLED: OUTBOX_STATUS_TERMINAL_FILL,
    OrderLifecycleState.REJECTED: OUTBOX_STATUS_TERMINAL_NON_FILL,
    OrderLifecycleState.CANCELLED: OUTBOX_STATUS_TERMINAL_NON_FILL,
    OrderLifecycleState.EXPIRED: OUTBOX_STATUS_TERMINAL_NON_FILL,
    OrderLifecycleState.FAILED: OUTBOX_STATUS_TERMINAL_NON_FILL,
}


def to_outbox_status(state: OrderLifecycleState) -> str:
    """Map a lifecycle state to the corresponding ``OUTBOX_STATUS_*`` constant.

    Falls back to ``OUTBOX_STATUS_ACKED`` for any unmapped state (should not
    happen given the exhaustive mapping above).
    """
    return _LIFECYCLE_TO_OUTBOX.get(state, OUTBOX_STATUS_ACKED)


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "OrderLifecycleState",
    "TERMINAL_ORDER_STATES",
    "TERMINAL_FILL_STATES",
    "TERMINAL_NON_FILL_STATES",
    "ACTIVE_ORDER_STATES",
    "VALID_ORDER_TRANSITIONS",
    "InvalidOrderTransition",
    "validate_order_transition",
    "is_terminal",
    "is_terminal_fill",
    "classify_broker_status",
    "to_outbox_status",
]
