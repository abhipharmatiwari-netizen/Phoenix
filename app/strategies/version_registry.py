"""Strategy versioning and deployment registry — PHX-STRAT-002.

Only approved, versioned strategy artifacts can run in paper/shadow/live.
Rollback works by re-activating a prior approved version.
Version lineage is queryable.

Deployment states (per strategy per environment):
  DRAFT → PENDING_APPROVAL → APPROVED → ACTIVE | ROLLED_BACK
  ACTIVE → ROLLING_BACK → ROLLED_BACK

A strategy can only be ACTIVE in one version per environment at a time.
Promotion requires approval from a second operator (maker-checker).
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


class DeploymentEnv(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


class VersionStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"


@dataclass
class StrategyVersion:
    version_id: str
    strategy_id: str
    version_tag: str          # e.g. "v1.0.0" or git SHA
    environment: DeploymentEnv
    config_snapshot: dict     # full config at time of registration
    author: str
    description: str = ""
    status: VersionStatus = VersionStatus.DRAFT
    approver: Optional[str] = None
    approved_at: Optional[str] = None
    activated_at: Optional[str] = None
    rolled_back_at: Optional[str] = None
    rollback_reason: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "strategy_id": self.strategy_id,
            "version_tag": self.version_tag,
            "environment": self.environment.value,
            "config_snapshot": self.config_snapshot,
            "author": self.author,
            "description": self.description,
            "status": self.status.value,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "activated_at": self.activated_at,
            "rolled_back_at": self.rolled_back_at,
            "rollback_reason": self.rollback_reason,
            "created_at": self.created_at,
        }


_STORE: dict[str, StrategyVersion] = {}  # version_id → StrategyVersion
_STORE_LOCK = threading.Lock()

# Active version per (strategy_id, env)
_ACTIVE: dict[tuple[str, str], str] = {}


def register_version(
    *,
    strategy_id: str,
    version_tag: str,
    environment: DeploymentEnv,
    config_snapshot: dict,
    author: str,
    description: str = "",
) -> StrategyVersion:
    """Register a new draft strategy version for approval."""
    version_id = uuid4().hex
    ver = StrategyVersion(
        version_id=version_id,
        strategy_id=strategy_id,
        version_tag=version_tag,
        environment=environment,
        config_snapshot=dict(config_snapshot),
        author=author,
        description=description,
        status=VersionStatus.DRAFT,
    )
    with _STORE_LOCK:
        _STORE[version_id] = ver
    emit_audit_event(
        actor=author,
        action="strategy_version_registered",
        resource_type="strategy_version",
        resource_id=version_id,
        after=ver.to_dict(),
    )
    logger.info(
        "strategy_version_registered strategy=%s version=%s env=%s version_id=%s",
        strategy_id, version_tag, environment.value, version_id,
    )
    return ver


def submit_for_approval(
    *,
    version_id: str,
    requester: str,
) -> tuple[bool, str]:
    """Move a DRAFT version to PENDING_APPROVAL."""
    with _STORE_LOCK:
        ver = _STORE.get(version_id)
        if ver is None:
            return False, "Version not found"
        if ver.status != VersionStatus.DRAFT:
            return False, f"Cannot submit — status is {ver.status.value}"
        ver.status = VersionStatus.PENDING_APPROVAL
    emit_audit_event(
        actor=requester,
        action="strategy_version_submitted_for_approval",
        resource_type="strategy_version",
        resource_id=version_id,
        after={"status": VersionStatus.PENDING_APPROVAL.value},
    )
    return True, "Submitted for approval"


def approve_version(
    *,
    version_id: str,
    approver: str,
) -> tuple[bool, str]:
    """Approve a pending version. Cannot be the author."""
    with _STORE_LOCK:
        ver = _STORE.get(version_id)
        if ver is None:
            return False, "Version not found"
        if ver.status != VersionStatus.PENDING_APPROVAL:
            return False, f"Cannot approve — status is {ver.status.value}"
        if ver.author == approver:
            return False, "Self-approval is not permitted"
        ver.status = VersionStatus.APPROVED
        ver.approver = approver
        ver.approved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    emit_audit_event(
        actor=approver,
        action="strategy_version_approved",
        resource_type="strategy_version",
        resource_id=version_id,
        after={"status": VersionStatus.APPROVED.value, "approver": approver},
    )
    logger.info("strategy_version_approved version_id=%s approver=%s", version_id, approver)
    return True, "Approved"


def reject_version(
    *,
    version_id: str,
    approver: str,
    reason: str = "",
) -> tuple[bool, str]:
    """Reject a pending version."""
    with _STORE_LOCK:
        ver = _STORE.get(version_id)
        if ver is None:
            return False, "Version not found"
        if ver.status != VersionStatus.PENDING_APPROVAL:
            return False, f"Cannot reject — status is {ver.status.value}"
        ver.status = VersionStatus.REJECTED
        ver.rollback_reason = reason
    emit_audit_event(
        actor=approver,
        action="strategy_version_rejected",
        resource_type="strategy_version",
        resource_id=version_id,
        after={"status": VersionStatus.REJECTED.value, "reason": reason},
    )
    return True, "Rejected"


def activate_version(
    *,
    version_id: str,
    actor: str,
) -> tuple[bool, str]:
    """Activate an approved version. Deactivates any currently active version."""
    with _STORE_LOCK:
        ver = _STORE.get(version_id)
        if ver is None:
            return False, "Version not found"
        if ver.status != VersionStatus.APPROVED:
            return False, f"Cannot activate — status is {ver.status.value}"
        key = (ver.strategy_id, ver.environment.value)
        prev_version_id = _ACTIVE.get(key)
        if prev_version_id and prev_version_id != version_id:
            prev = _STORE.get(prev_version_id)
            if prev and prev.status == VersionStatus.ACTIVE:
                prev.status = VersionStatus.ROLLED_BACK
                prev.rolled_back_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                prev.rollback_reason = f"Superseded by {version_id}"
        ver.status = VersionStatus.ACTIVE
        ver.activated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _ACTIVE[key] = version_id

    emit_audit_event(
        actor=actor,
        action="strategy_version_activated",
        resource_type="strategy_version",
        resource_id=version_id,
        after={"status": VersionStatus.ACTIVE.value, "env": ver.environment.value},
    )
    logger.info(
        "strategy_version_activated strategy=%s env=%s version_id=%s actor=%s",
        ver.strategy_id, ver.environment.value, version_id, actor,
    )
    return True, "Activated"


def rollback_to_version(
    *,
    version_id: str,
    actor: str,
    reason: str = "",
) -> tuple[bool, str]:
    """Rollback to a previously approved version."""
    with _STORE_LOCK:
        ver = _STORE.get(version_id)
        if ver is None:
            return False, "Version not found"
        if ver.status not in (VersionStatus.APPROVED, VersionStatus.ROLLED_BACK):
            return False, f"Cannot rollback to — status is {ver.status.value}"
        ver.status = VersionStatus.APPROVED  # Re-approve so activate can proceed

    ok, msg = activate_version(version_id=version_id, actor=actor)
    if ok:
        emit_audit_event(
            actor=actor,
            action="strategy_version_rollback",
            resource_type="strategy_version",
            resource_id=version_id,
            after={"reason": reason},
        )
    return ok, msg


def get_active_version(
    strategy_id: str,
    environment: DeploymentEnv,
) -> Optional[StrategyVersion]:
    with _STORE_LOCK:
        vid = _ACTIVE.get((strategy_id, environment.value))
        if vid is None:
            return None
        return _STORE.get(vid)


def require_active_version(
    strategy_id: str,
    environment: DeploymentEnv,
) -> StrategyVersion:
    """Return the active version or raise RuntimeError."""
    ver = get_active_version(strategy_id, environment)
    if ver is None:
        raise RuntimeError(
            f"No active approved version for strategy={strategy_id} env={environment.value}. "
            "Strategy must be registered, approved, and activated before use."
        )
    return ver


def list_versions(
    strategy_id: Optional[str] = None,
    environment: Optional[DeploymentEnv] = None,
    status: Optional[VersionStatus] = None,
) -> list[dict[str, Any]]:
    with _STORE_LOCK:
        versions = list(_STORE.values())
    filtered = [
        v for v in versions
        if (strategy_id is None or v.strategy_id == strategy_id)
        and (environment is None or v.environment == environment)
        and (status is None or v.status == status)
    ]
    filtered.sort(key=lambda v: v.created_at, reverse=True)
    return [v.to_dict() for v in filtered]


__all__ = [
    "DeploymentEnv",
    "StrategyVersion",
    "VersionStatus",
    "activate_version",
    "approve_version",
    "get_active_version",
    "list_versions",
    "register_version",
    "reject_version",
    "rollback_to_version",
    "require_active_version",
    "submit_for_approval",
]
