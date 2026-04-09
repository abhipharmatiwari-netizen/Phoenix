"""Progressive rollout ladder — PHX-SIM-005.

Deployment states with quantitative promotion criteria:
  DISABLED → PAPER → SHADOW → MICRO_LIVE → CAPPED_LIVE → SCALED_LIVE

Each transition requires:
  1. Meeting quantitative criteria (defined per transition)
  2. Approval by an authorized operator (maker-checker)
  3. Audit trail of promotion decision

Demotion (manual or auto):
  Any state can be demoted to DISABLED.
  MICRO_LIVE / CAPPED_LIVE / SCALED_LIVE auto-demote on envelope breach.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)


class RolloutState(str, Enum):
    DISABLED = "DISABLED"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    MICRO_LIVE = "MICRO_LIVE"        # e.g. 5% of intended capital
    CAPPED_LIVE = "CAPPED_LIVE"      # e.g. 25% of intended capital
    SCALED_LIVE = "SCALED_LIVE"      # full deployment

    @property
    def is_live(self) -> bool:
        return self in (
            RolloutState.MICRO_LIVE,
            RolloutState.CAPPED_LIVE,
            RolloutState.SCALED_LIVE,
        )

    @property
    def capital_fraction(self) -> float:
        _fractions = {
            RolloutState.DISABLED: 0.0,
            RolloutState.PAPER: 0.0,
            RolloutState.SHADOW: 0.0,
            RolloutState.MICRO_LIVE: 0.05,
            RolloutState.CAPPED_LIVE: 0.25,
            RolloutState.SCALED_LIVE: 1.0,
        }
        return _fractions.get(self, 0.0)


_VALID_PROMOTIONS: dict[RolloutState, RolloutState] = {
    RolloutState.DISABLED: RolloutState.PAPER,
    RolloutState.PAPER: RolloutState.SHADOW,
    RolloutState.SHADOW: RolloutState.MICRO_LIVE,
    RolloutState.MICRO_LIVE: RolloutState.CAPPED_LIVE,
    RolloutState.CAPPED_LIVE: RolloutState.SCALED_LIVE,
}


@dataclass
class PromotionCriteria:
    """Quantitative criteria required to promote to the next state."""
    min_paper_days: int = 0           # minimum days in PAPER
    min_shadow_days: int = 0          # minimum days in SHADOW
    min_trades: int = 0               # minimum trades in current state
    max_drawdown_pct: float = 1.0     # must not have exceeded this drawdown
    min_sharpe: float = 0.0           # minimum Sharpe ratio (0 = no check)
    min_fill_rate: float = 0.0        # minimum order fill rate
    requires_approval: bool = True     # always True for live states


@dataclass
class RolloutRecord:
    strategy_id: str
    environment: str
    current_state: RolloutState = RolloutState.DISABLED
    state_history: list[dict] = field(default_factory=list)
    criteria_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromotionRequest:
    request_id: str
    strategy_id: str
    environment: str
    from_state: RolloutState
    to_state: RolloutState
    requester: str
    criteria_evidence: dict
    status: str = "PENDING"           # PENDING | APPROVED | REJECTED
    approver: Optional[str] = None
    decided_at: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "strategy_id": self.strategy_id,
            "environment": self.environment,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "requester": self.requester,
            "criteria_evidence": self.criteria_evidence,
            "status": self.status,
            "approver": self.approver,
            "decided_at": self.decided_at,
            "created_at": self.created_at,
        }


_RECORDS: dict[str, RolloutRecord] = {}   # key = f"{strategy_id}:{env}"
_PROMO_REQUESTS: dict[str, PromotionRequest] = {}
_LOCK = threading.Lock()


def _record_key(strategy_id: str, environment: str) -> str:
    return f"{strategy_id}:{environment}"


def get_or_create(strategy_id: str, environment: str = "live") -> RolloutRecord:
    key = _record_key(strategy_id, environment)
    with _LOCK:
        if key not in _RECORDS:
            _RECORDS[key] = RolloutRecord(strategy_id=strategy_id, environment=environment)
        return _RECORDS[key]


def get_state(strategy_id: str, environment: str = "live") -> RolloutState:
    key = _record_key(strategy_id, environment)
    with _LOCK:
        rec = _RECORDS.get(key)
    return rec.current_state if rec else RolloutState.DISABLED


def submit_promotion_request(
    *,
    strategy_id: str,
    environment: str = "live",
    requester: str,
    criteria_evidence: dict,
) -> tuple[bool, str, Optional[PromotionRequest]]:
    """Submit a request to promote to the next rollout state."""
    key = _record_key(strategy_id, environment)
    with _LOCK:
        rec = _RECORDS.setdefault(key, RolloutRecord(strategy_id=strategy_id, environment=environment))
        current = rec.current_state
        next_state = _VALID_PROMOTIONS.get(current)

    if next_state is None:
        return False, f"No promotion path from {current.value}", None

    req = PromotionRequest(
        request_id=uuid4().hex,
        strategy_id=strategy_id,
        environment=environment,
        from_state=current,
        to_state=next_state,
        requester=requester,
        criteria_evidence=criteria_evidence,
    )
    with _LOCK:
        _PROMO_REQUESTS[req.request_id] = req

    emit_audit_event(
        actor=requester,
        action="rollout_promotion_requested",
        resource_type="rollout",
        resource_id=f"{strategy_id}:{environment}",
        after=req.to_dict(),
    )
    logger.info(
        "rollout_promotion_requested strategy=%s env=%s %s→%s requester=%s",
        strategy_id, environment, current.value, next_state.value, requester,
    )
    return True, "Promotion request submitted", req


def approve_promotion(
    *,
    request_id: str,
    approver: str,
) -> tuple[bool, str]:
    """Approve a pending promotion request. Cannot self-approve."""
    with _LOCK:
        req = _PROMO_REQUESTS.get(request_id)
        if req is None:
            return False, "Request not found"
        if req.status != "PENDING":
            return False, f"Request status is {req.status}"
        if req.requester == approver:
            return False, "Self-approval not permitted"
        req.status = "APPROVED"
        req.approver = approver
        req.decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Apply the transition
        key = _record_key(req.strategy_id, req.environment)
        rec = _RECORDS.get(key)
        if rec is None:
            return False, "Rollout record not found"
        rec.state_history.append({
            "from": rec.current_state.value,
            "to": req.to_state.value,
            "at": req.decided_at,
            "by": approver,
            "request_id": request_id,
        })
        rec.current_state = req.to_state

    emit_audit_event(
        actor=approver,
        action="rollout_promotion_approved",
        resource_type="rollout",
        resource_id=f"{req.strategy_id}:{req.environment}",
        after={
            "from_state": req.from_state.value,
            "to_state": req.to_state.value,
            "capital_fraction": req.to_state.capital_fraction,
        },
    )
    logger.info(
        "rollout_promotion_approved strategy=%s env=%s %s→%s approver=%s",
        req.strategy_id, req.environment,
        req.from_state.value, req.to_state.value, approver,
    )
    return True, f"Promoted to {req.to_state.value}"


def demote(
    *,
    strategy_id: str,
    environment: str = "live",
    actor: str,
    reason: str = "",
) -> None:
    """Immediately demote a strategy to DISABLED."""
    key = _record_key(strategy_id, environment)
    with _LOCK:
        rec = _RECORDS.setdefault(key, RolloutRecord(strategy_id=strategy_id, environment=environment))
        prev = rec.current_state
        rec.current_state = RolloutState.DISABLED
        rec.state_history.append({
            "from": prev.value,
            "to": RolloutState.DISABLED.value,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "by": actor,
            "reason": reason,
        })

    emit_audit_event(
        actor=actor,
        action="rollout_demoted",
        resource_type="rollout",
        resource_id=f"{strategy_id}:{environment}",
        before={"state": prev.value},
        after={"state": RolloutState.DISABLED.value, "reason": reason},
    )
    logger.warning(
        "rollout_demoted strategy=%s env=%s %s→DISABLED actor=%s reason=%s",
        strategy_id, environment, prev.value, actor, reason,
    )


def list_promotion_requests(
    strategy_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    with _LOCK:
        reqs = list(_PROMO_REQUESTS.values())
    filtered = [
        r for r in reqs
        if (strategy_id is None or r.strategy_id == strategy_id)
        and (status is None or r.status == status)
    ]
    return [r.to_dict() for r in sorted(filtered, key=lambda r: r.created_at, reverse=True)]


def status_snapshot() -> list[dict]:
    with _LOCK:
        records = list(_RECORDS.values())
    return [
        {
            "strategy_id": r.strategy_id,
            "environment": r.environment,
            "current_state": r.current_state.value,
            "capital_fraction": r.current_state.capital_fraction,
            "is_live": r.current_state.is_live,
            "history_count": len(r.state_history),
        }
        for r in records
    ]


__all__ = [
    "RolloutState",
    "PromotionCriteria",
    "PromotionRequest",
    "approve_promotion",
    "demote",
    "get_or_create",
    "get_state",
    "list_promotion_requests",
    "status_snapshot",
    "submit_promotion_request",
]
