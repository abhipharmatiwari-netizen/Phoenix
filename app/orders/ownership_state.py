"""
Explicit contract-ownership state machine (Architecture §3.4).

Every position tracked by the system has an OwnershipRecord whose lifecycle
is governed by a finite set of states and validated transitions.  The state
machine enforces that:

  * Only one strategy can own a given OwnershipKey at a time.
  * Fresh entries are blocked while the key is in RECONCILING or ORPHAN_REVIEW.
  * Every transition is auditable via *state_reason*.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, Optional, Set


# ---------------------------------------------------------------------------
# Ownership states
# ---------------------------------------------------------------------------

class OwnershipState(str, enum.Enum):
    """Possible states for a contract-ownership record."""

    NONE = "NONE"
    PENDING_LOCK = "PENDING_LOCK"
    OWNED = "OWNED"
    RELEASING = "RELEASING"
    RECONCILING = "RECONCILING"
    ORPHAN_REVIEW = "ORPHAN_REVIEW"


# ---------------------------------------------------------------------------
# Blocking rules
# ---------------------------------------------------------------------------

ENTRY_BLOCKING_OWNERSHIP_STATES: FrozenSet[OwnershipState] = frozenset({
    OwnershipState.RECONCILING,
    OwnershipState.ORPHAN_REVIEW,
})


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

VALID_OWNERSHIP_TRANSITIONS: Dict[OwnershipState, Set[OwnershipState]] = {
    OwnershipState.NONE: {
        OwnershipState.PENDING_LOCK,
        OwnershipState.RECONCILING,       # unexpected divergence
    },
    OwnershipState.PENDING_LOCK: {
        OwnershipState.OWNED,             # fill confirmation
        OwnershipState.NONE,              # reject / cancel / fail
        OwnershipState.RECONCILING,       # ambiguous evidence or unexpected divergence
    },
    OwnershipState.OWNED: {
        OwnershipState.RELEASING,         # exit initiation
        OwnershipState.RECONCILING,       # ambiguous evidence or unexpected divergence
    },
    OwnershipState.RELEASING: {
        OwnershipState.NONE,              # confirmed flat
        OwnershipState.RECONCILING,       # unexpected divergence
    },
    OwnershipState.RECONCILING: {
        OwnershipState.OWNED,             # resolution: position confirmed live
        OwnershipState.NONE,              # resolution: confirmed flat (strong evidence)
        OwnershipState.ORPHAN_REVIEW,     # timeout / unresolvable ambiguity
    },
    OwnershipState.ORPHAN_REVIEW: {
        OwnershipState.OWNED,             # operator adopt
        OwnershipState.NONE,             # operator flatten / suppress
        OwnershipState.RECONCILING,       # unexpected divergence
    },
}


# ---------------------------------------------------------------------------
# Transition validator
# ---------------------------------------------------------------------------

class InvalidOwnershipTransition(Exception):
    """Raised when a requested state transition is not permitted."""

    def __init__(self, current: OwnershipState, target: OwnershipState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"Invalid ownership transition: {current.value} -> {target.value}"
        )


def validate_ownership_transition(
    current: OwnershipState,
    target: OwnershipState,
) -> None:
    """Raise ``InvalidOwnershipTransition`` if *current -> target* is illegal.

    A no-op transition (current == target) is always rejected; staying in the
    same state is not a valid transition.
    """
    if current == target:
        raise InvalidOwnershipTransition(current, target)

    allowed = VALID_OWNERSHIP_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidOwnershipTransition(current, target)


# ---------------------------------------------------------------------------
# Orphan-review operator decisions
# ---------------------------------------------------------------------------

class OrphanReviewDecision(str, enum.Enum):
    """Actions an operator may take when a key enters ORPHAN_REVIEW."""

    ADOPT = "ADOPT"
    FLATTEN = "FLATTEN"
    SUPPRESS = "SUPPRESS"
    CONTINUE_OBSERVING = "CONTINUE_OBSERVING"


# ---------------------------------------------------------------------------
# Ownership record
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class OwnershipRecord:
    """Tracks the ownership lifecycle of a single contract key.

    Parameters
    ----------
    ownership_key:
        Canonical composite key (e.g. ``"ACCOUNT:SYMBOL:EXPIRY"``).
    state:
        Current ownership state.
    state_reason:
        Human-readable reason for the most recent state transition.
    authority_path:
        ``"legacy"`` or ``"hub"`` — identifies the control path that governs
        this record.
    owner_strategy_id:
        Strategy currently owning (or attempting to own) the key.
    position_id:
        Broker-side position identifier, when known.
    opened_by_order_id:
        The order that originally acquired the lock.
    acquired_at:
        Timestamp when the record first moved to OWNED.
    last_evidence_at:
        Timestamp of the most recent broker evidence processed for this key.
    break_glass_override_id:
        If an operator override was applied, its tracking identifier.
    updated_at:
        Wall-clock time of the last mutation.
    """

    ownership_key: str
    state: OwnershipState = OwnershipState.NONE
    state_reason: str = ""
    authority_path: str = "legacy"

    owner_strategy_id: Optional[str] = None
    position_id: Optional[str] = None
    opened_by_order_id: Optional[str] = None
    acquired_at: Optional[datetime] = None
    last_evidence_at: Optional[datetime] = None
    break_glass_override_id: Optional[str] = None

    updated_at: datetime = field(default_factory=_utcnow)

    # -- helpers -------------------------------------------------------------

    def blocks_new_entry(self) -> bool:
        """Return True if the current state prevents fresh entries."""
        return self.state in ENTRY_BLOCKING_OWNERSHIP_STATES

    def transition_to(
        self,
        target: OwnershipState,
        reason: str,
        *,
        override_id: Optional[str] = None,
    ) -> None:
        """Validate and apply a state transition in-place.

        Raises ``InvalidOwnershipTransition`` on illegal moves.
        """
        validate_ownership_transition(self.state, target)
        self.state = target
        self.state_reason = reason
        self.updated_at = _utcnow()
        if override_id is not None:
            self.break_glass_override_id = override_id
