"""
Internal position state machine per Architecture section 3.3.

Defines the canonical set of position lifecycle states, valid transitions,
and the InternalPosition dataclass that carries authoritative position data
through the system.

Design invariants:
  - FLAT is the only terminal state and is reached *only* when internal
    quantity, broker evidence, and lifecycle evidence all agree.
  - Ambiguous evidence routes to RECONCILING, never silently to FLAT.
  - DEGRADED captures broken remap / exit failures for operator attention.
  - MANUAL_REVIEW and FORCED_FLATTENING require human convergence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, FrozenSet, Optional, Set


# ---------------------------------------------------------------------------
# Position state enum
# ---------------------------------------------------------------------------

class PositionState(str, Enum):
    """All allowed internal position states."""

    NONE = "NONE"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPENING = "OPENING"
    OPEN = "OPEN"
    PARTIALLY_EXITED = "PARTIALLY_EXITED"
    EXIT_PENDING = "EXIT_PENDING"
    FLAT_PENDING_CONFIRMATION = "FLAT_PENDING_CONFIRMATION"
    FLAT = "FLAT"
    ADOPTED = "ADOPTED"
    FORCED_FLATTENING = "FORCED_FLATTENING"
    RECONCILING = "RECONCILING"
    DEGRADED = "DEGRADED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RECOVERY_PENDING = "RECOVERY_PENDING"


# ---------------------------------------------------------------------------
# State classification sets
# ---------------------------------------------------------------------------

TERMINAL_POSITION_STATES: FrozenSet[PositionState] = frozenset({
    PositionState.FLAT,
})

ENTRY_BLOCKING_STATES: FrozenSet[PositionState] = frozenset({
    PositionState.DEGRADED,
    PositionState.RECONCILING,
    PositionState.MANUAL_REVIEW,
    PositionState.FORCED_FLATTENING,
    PositionState.RECOVERY_PENDING,
})

EXIT_ALLOWED_STATES: FrozenSet[PositionState] = frozenset({
    PositionState.OPEN,
    PositionState.PARTIALLY_EXITED,
    PositionState.ADOPTED,
    PositionState.FORCED_FLATTENING,
})

# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: Dict[PositionState, Set[PositionState]] = {
    # Normal entry flow: NONE -> ENTRY_PENDING -> OPENING -> OPEN
    PositionState.NONE: {
        PositionState.ENTRY_PENDING,
        PositionState.ADOPTED,          # broker-adopted via reconciliation
        PositionState.RECONCILING,       # ambiguous evidence
    },
    PositionState.ENTRY_PENDING: {
        PositionState.OPENING,
        PositionState.RECONCILING,       # ambiguous evidence
        PositionState.DEGRADED,          # failed remap / broken entry
        PositionState.MANUAL_REVIEW,     # break-glass
        PositionState.NONE,              # entry rejected / cancelled
    },
    PositionState.OPENING: {
        PositionState.OPEN,              # fill evidence is durable
        PositionState.RECONCILING,       # ambiguous evidence
        PositionState.DEGRADED,          # broken entry
        PositionState.MANUAL_REVIEW,     # break-glass
    },
    # Normal exit flow: OPEN -> EXIT_PENDING -> FLAT_PENDING_CONFIRMATION -> FLAT
    PositionState.OPEN: {
        PositionState.PARTIALLY_EXITED,
        PositionState.EXIT_PENDING,
        PositionState.FORCED_FLATTENING, # manual forced flatten
        PositionState.RECONCILING,       # ambiguous evidence
        PositionState.DEGRADED,          # broken exit
        PositionState.MANUAL_REVIEW,     # break-glass
    },
    PositionState.PARTIALLY_EXITED: {
        PositionState.EXIT_PENDING,
        PositionState.OPEN,              # partial exit reversed / re-evaluated
        PositionState.FORCED_FLATTENING,
        PositionState.RECONCILING,
        PositionState.DEGRADED,
        PositionState.MANUAL_REVIEW,
    },
    PositionState.EXIT_PENDING: {
        PositionState.FLAT_PENDING_CONFIRMATION,
        PositionState.PARTIALLY_EXITED,  # partial fill on exit
        PositionState.RECONCILING,
        PositionState.DEGRADED,          # broken exit
        PositionState.MANUAL_REVIEW,
    },
    PositionState.FLAT_PENDING_CONFIRMATION: {
        PositionState.FLAT,              # all evidence agrees
        PositionState.RECONCILING,       # evidence disagrees
        PositionState.DEGRADED,
        PositionState.MANUAL_REVIEW,
    },
    # FLAT is terminal -- no outbound transitions
    PositionState.FLAT: set(),
    PositionState.ADOPTED: {
        PositionState.OPEN,              # adoption accepted, now managed
        PositionState.EXIT_PENDING,      # immediate exit of adopted position
        PositionState.FORCED_FLATTENING,
        PositionState.RECONCILING,
        PositionState.DEGRADED,
        PositionState.MANUAL_REVIEW,
    },
    PositionState.FORCED_FLATTENING: {
        PositionState.EXIT_PENDING,
        PositionState.FLAT_PENDING_CONFIRMATION,
        PositionState.RECONCILING,
        PositionState.DEGRADED,
        PositionState.MANUAL_REVIEW,
    },
    PositionState.RECONCILING: {
        PositionState.OPEN,              # reconciliation resolved to open
        PositionState.FLAT_PENDING_CONFIRMATION,
        PositionState.ADOPTED,           # reconciliation found broker position
        PositionState.DEGRADED,
        PositionState.MANUAL_REVIEW,
        PositionState.FORCED_FLATTENING,
    },
    PositionState.DEGRADED: {
        PositionState.RECONCILING,       # operator triggers re-reconciliation
        PositionState.MANUAL_REVIEW,
        PositionState.FORCED_FLATTENING,
    },
    PositionState.MANUAL_REVIEW: {
        PositionState.OPEN,              # operator resolves to open
        PositionState.FLAT_PENDING_CONFIRMATION,
        PositionState.FORCED_FLATTENING,
        PositionState.RECONCILING,
        PositionState.DEGRADED,
    },
    PositionState.RECOVERY_PENDING: {
        PositionState.OPEN,              # broker confirms position open
        PositionState.FLAT,              # broker confirms no position
        PositionState.RECONCILING,       # ambiguous evidence
        PositionState.DEGRADED,          # recovery failed
        PositionState.ADOPTED,           # broker-adopted position
        PositionState.MANUAL_REVIEW,     # operator intervention needed
        PositionState.NONE,              # no prior position found
    },
}


# ---------------------------------------------------------------------------
# Transition validator
# ---------------------------------------------------------------------------

def validate_transition(current: PositionState, target: PositionState) -> bool:
    """Validate that *current* -> *target* is an allowed state transition.

    Returns ``True`` when the transition is valid.

    Raises:
        ValueError: If *current* or *target* is not a ``PositionState``, or
            the transition is not allowed by the state machine.
    """
    if not isinstance(current, PositionState):
        raise ValueError(f"current is not a PositionState: {current!r}")
    if not isinstance(target, PositionState):
        raise ValueError(f"target is not a PositionState: {target!r}")
    if current == target:
        raise ValueError(
            f"Self-transition not allowed: {current.value} -> {target.value}"
        )

    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(
            f"Illegal transition: {current.value} -> {target.value}. "
            f"Allowed from {current.value}: "
            f"{{{', '.join(s.value for s in sorted(allowed, key=lambda s: s.value))}}}."
        )
    return True


# ---------------------------------------------------------------------------
# Internal position dataclass
# ---------------------------------------------------------------------------

@dataclass
class InternalPosition:
    """Authoritative internal position record.

    Carries all fields required by Architecture section 3.3 to track a single
    position through its full lifecycle -- from entry intent through
    reconciliation-confirmed flat.
    """

    # --- Identity ----------------------------------------------------------
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = ""
    account_id: str = ""
    strategy_id: str = ""
    ownership_key: str = ""
    contract_key: str = ""

    # --- Quantity / price --------------------------------------------------
    side: str = ""                          # "BUY" | "SELL"
    net_qty: float = 0.0
    filled_qty_open: float = 0.0
    filled_qty_close: float = 0.0
    avg_open_price: float = 0.0
    avg_close_price: float = 0.0

    # --- PnL ---------------------------------------------------------------
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # --- State -------------------------------------------------------------
    position_state: PositionState = PositionState.NONE
    state_reason: str = ""

    # --- Lineage -----------------------------------------------------------
    opened_by_order_id: str = ""

    # --- Timestamps --------------------------------------------------------
    last_evidence_at: Optional[datetime] = None
    last_reconciled_at: Optional[datetime] = None

    # -- Convenience --------------------------------------------------------

    def transition_to(self, target: PositionState, reason: str = "") -> None:
        """Attempt to move this position to *target* state.

        Validates the transition, then mutates ``position_state`` and
        ``state_reason`` in place.

        Raises:
            ValueError: If the transition is not allowed.
        """
        validate_transition(self.position_state, target)
        self.position_state = target
        self.state_reason = reason

    @property
    def is_terminal(self) -> bool:
        """Return True when the position is in a terminal state."""
        return self.position_state in TERMINAL_POSITION_STATES

    @property
    def is_entry_blocked(self) -> bool:
        """Return True when the position state blocks new entries."""
        return self.position_state in ENTRY_BLOCKING_STATES

    @property
    def is_exit_allowed(self) -> bool:
        """Return True when exit orders may be submitted."""
        return self.position_state in EXIT_ALLOWED_STATES
