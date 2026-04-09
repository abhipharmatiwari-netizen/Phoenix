"""Config/strategy approval workflow engine — PHX-AUD-003.

Production config or strategy changes require an approval trail:
  - requester submits a change proposal
  - approver (different identity) approves or rejects
  - on approval, the change is applied and the full diff is stored
  - rollback to prior version is supported

All approval decisions are emitted as audit events.

States: PENDING → APPROVED | REJECTED | SUPERSEDED | WITHDRAWN
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


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


class ChangeKind(str, Enum):
    STRATEGY_CONFIG = "strategy_config"
    CAPITAL_LIMIT = "capital_limit"
    RISK_POLICY = "risk_policy"
    FEATURE_FLAG = "feature_flag"
    STRATEGY_ENABLE = "strategy_enable"
    STRATEGY_DISABLE = "strategy_disable"
    GENERAL_CONFIG = "general_config"


@dataclass
class ApprovalRecord:
    request_id: str
    change_kind: ChangeKind
    resource_id: str          # strategy ID, config key, etc.
    requester: str
    description: str
    before: Any
    after: Any
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver: Optional[str] = None
    approver_note: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    decided_at: Optional[str] = None
    # Optional callback to apply the change on approval
    # (not stored; wired at runtime)
    _apply_fn: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "change_kind": self.change_kind.value,
            "resource_id": self.resource_id,
            "requester": self.requester,
            "description": self.description,
            "before": self.before,
            "after": self.after,
            "status": self.status.value,
            "approver": self.approver,
            "approver_note": self.approver_note,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
        }


_STORE: dict[str, ApprovalRecord] = {}
_STORE_LOCK = threading.Lock()


def submit_change_request(
    *,
    change_kind: ChangeKind,
    resource_id: str,
    requester: str,
    description: str,
    before: Any,
    after: Any,
    apply_fn: Any = None,
) -> ApprovalRecord:
    """Submit a change request for approval.

    Any pending requests for the same resource_id+change_kind are superseded.
    """
    request_id = uuid4().hex
    rec = ApprovalRecord(
        request_id=request_id,
        change_kind=change_kind,
        resource_id=resource_id,
        requester=requester,
        description=description,
        before=before,
        after=after,
    )
    rec._apply_fn = apply_fn

    with _STORE_LOCK:
        # Supersede any existing pending requests for same resource
        for existing in _STORE.values():
            if (
                existing.resource_id == resource_id
                and existing.change_kind == change_kind
                and existing.status == ApprovalStatus.PENDING
                and existing.request_id != request_id
            ):
                existing.status = ApprovalStatus.SUPERSEDED
                existing.decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _STORE[request_id] = rec

    emit_audit_event(
        actor=requester,
        action="change_request_submitted",
        resource_type="approval",
        resource_id=request_id,
        before=before,
        after=after,
        metadata={
            "change_kind": change_kind.value,
            "resource_id": resource_id,
            "description": description,
        },
    )
    logger.info(
        "change_request_submitted request_id=%s kind=%s resource=%s requester=%s",
        request_id, change_kind.value, resource_id, requester,
    )
    return rec


def approve_change_request(
    *,
    request_id: str,
    approver: str,
    note: str = "",
) -> tuple[bool, str]:
    """Approve a pending change request. Cannot self-approve.

    Returns (success, message). Calls apply_fn if provided.
    """
    with _STORE_LOCK:
        rec = _STORE.get(request_id)
        if rec is None:
            return False, "Request not found"
        if rec.status != ApprovalStatus.PENDING:
            return False, f"Request is in state {rec.status.value}"
        if rec.requester == approver:
            return False, "Self-approval is not permitted"
        rec.status = ApprovalStatus.APPROVED
        rec.approver = approver
        rec.approver_note = note
        rec.decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        apply_fn = rec._apply_fn

    # Apply the change outside the lock
    applied_ok = True
    apply_error = ""
    if apply_fn is not None:
        try:
            apply_fn(rec.after)
        except Exception as exc:
            applied_ok = False
            apply_error = str(exc)
            logger.exception("change_apply_failed request_id=%s", request_id)

    emit_audit_event(
        actor=approver,
        action="change_request_approved",
        resource_type="approval",
        resource_id=request_id,
        before=rec.before,
        after=rec.after,
        metadata={
            "change_kind": rec.change_kind.value,
            "resource_id": rec.resource_id,
            "requester": rec.requester,
            "note": note,
            "applied_ok": applied_ok,
            "apply_error": apply_error,
        },
    )
    logger.info(
        "change_request_approved request_id=%s approver=%s applied=%s",
        request_id, approver, applied_ok,
    )
    return True, "Approved" if applied_ok else f"Approved but apply failed: {apply_error}"


def reject_change_request(
    *,
    request_id: str,
    approver: str,
    note: str = "",
) -> tuple[bool, str]:
    """Reject a pending change request."""
    with _STORE_LOCK:
        rec = _STORE.get(request_id)
        if rec is None:
            return False, "Request not found"
        if rec.status != ApprovalStatus.PENDING:
            return False, f"Request is in state {rec.status.value}"
        rec.status = ApprovalStatus.REJECTED
        rec.approver = approver
        rec.approver_note = note
        rec.decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    emit_audit_event(
        actor=approver,
        action="change_request_rejected",
        resource_type="approval",
        resource_id=request_id,
        before=rec.before,
        after=rec.after,
        metadata={
            "change_kind": rec.change_kind.value,
            "resource_id": rec.resource_id,
            "requester": rec.requester,
            "note": note,
        },
    )
    logger.info("change_request_rejected request_id=%s approver=%s", request_id, approver)
    return True, "Rejected"


def withdraw_change_request(
    *,
    request_id: str,
    requester: str,
) -> tuple[bool, str]:
    """Withdraw a pending request (only the requester can withdraw)."""
    with _STORE_LOCK:
        rec = _STORE.get(request_id)
        if rec is None:
            return False, "Request not found"
        if rec.status != ApprovalStatus.PENDING:
            return False, f"Request is in state {rec.status.value}"
        if rec.requester != requester:
            return False, "Only the requester can withdraw"
        rec.status = ApprovalStatus.WITHDRAWN
        rec.decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    emit_audit_event(
        actor=requester,
        action="change_request_withdrawn",
        resource_type="approval",
        resource_id=request_id,
        metadata={"change_kind": rec.change_kind.value, "resource_id": rec.resource_id},
    )
    return True, "Withdrawn"


def list_requests(
    *,
    status: Optional[ApprovalStatus] = None,
    change_kind: Optional[ChangeKind] = None,
    resource_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return approval requests, newest first."""
    with _STORE_LOCK:
        records = list(_STORE.values())
    filtered = [
        r for r in records
        if (status is None or r.status == status)
        and (change_kind is None or r.change_kind == change_kind)
        and (resource_id is None or r.resource_id == resource_id)
    ]
    filtered.sort(key=lambda r: r.created_at, reverse=True)
    return [r.to_dict() for r in filtered[:limit]]


def get_request(request_id: str) -> Optional[dict[str, Any]]:
    with _STORE_LOCK:
        rec = _STORE.get(request_id)
    return rec.to_dict() if rec else None


__all__ = [
    "ApprovalRecord",
    "ApprovalStatus",
    "ChangeKind",
    "approve_change_request",
    "get_request",
    "list_requests",
    "reject_change_request",
    "submit_change_request",
    "withdraw_change_request",
]
