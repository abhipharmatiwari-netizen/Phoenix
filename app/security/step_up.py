"""Step-up approval gate for dangerous/privileged actions — PHX-SEC-006.

Dangerous actions (kill-switch clear, strategy enable/disable, capital-limit
changes, break-glass) require either:
  1. A time-limited step-up token issued by re-authentication, OR
  2. A maker-checker approval record signed by a second admin.

Step-up tokens are short-lived (5 minutes default), single-use, and bound to
a specific action class.  They are stored in-memory and optionally in Postgres.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)

_STEP_UP_TTL_SECONDS = 5 * 60  # 5 minutes

# ---------------------------------------------------------------------------
# Dangerous action classes
# ---------------------------------------------------------------------------

class DangerousActionClass(str, Enum):
    KILL_SWITCH_CLEAR = "kill_switch_clear"
    KILL_SWITCH_REARM = "kill_switch_rearm"
    STRATEGY_ENABLE = "strategy_enable"
    STRATEGY_DISABLE = "strategy_disable"
    CAPITAL_LIMIT_CHANGE = "capital_limit_change"
    BREAK_GLASS = "break_glass"
    USER_PROMOTE = "user_promote"
    CONFIG_CHANGE = "config_change"


_ACTION_CLASS_LABELS: dict[DangerousActionClass, str] = {
    DangerousActionClass.KILL_SWITCH_CLEAR: "Kill-switch clear",
    DangerousActionClass.KILL_SWITCH_REARM: "Kill-switch rearm",
    DangerousActionClass.STRATEGY_ENABLE: "Strategy enable",
    DangerousActionClass.STRATEGY_DISABLE: "Strategy disable",
    DangerousActionClass.CAPITAL_LIMIT_CHANGE: "Capital limit change",
    DangerousActionClass.BREAK_GLASS: "Break-glass access",
    DangerousActionClass.USER_PROMOTE: "User role promotion",
    DangerousActionClass.CONFIG_CHANGE: "Configuration change",
}


# ---------------------------------------------------------------------------
# Step-up token store
# ---------------------------------------------------------------------------

@dataclass
class StepUpToken:
    token_id: str
    actor: str
    action_class: DangerousActionClass
    resource_id: str
    issued_at: float
    expires_at: float
    used: bool = False
    approver: Optional[str] = None  # for maker-checker: second approver's identity


_STORE: dict[str, StepUpToken] = {}
_STORE_LOCK = threading.Lock()


def issue_step_up_token(
    *,
    actor: str,
    action_class: DangerousActionClass,
    resource_id: str = "",
    ttl_seconds: int = _STEP_UP_TTL_SECONDS,
) -> StepUpToken:
    """Issue a step-up token for an actor/action.  Emits an audit event."""
    token_id = uuid4().hex
    now = time.time()
    tok = StepUpToken(
        token_id=token_id,
        actor=actor,
        action_class=action_class,
        resource_id=str(resource_id or ""),
        issued_at=now,
        expires_at=now + ttl_seconds,
    )
    with _STORE_LOCK:
        _STORE[token_id] = tok
    emit_audit_event(
        actor=actor,
        action="step_up_issued",
        resource_type="step_up_token",
        resource_id=token_id,
        after={
            "action_class": action_class.value,
            "resource_id": resource_id,
            "expires_at": tok.expires_at,
        },
    )
    logger.info(
        "Step-up token issued actor=%s action_class=%s token_id=%s",
        actor, action_class.value, token_id,
    )
    return tok


def consume_step_up_token(
    *,
    token_id: str,
    actor: str,
    action_class: DangerousActionClass,
    resource_id: str = "",
) -> bool:
    """Consume a step-up token.  Returns True if valid and consumed.

    Validates: token exists, not used, not expired, actor matches, action_class matches.
    resource_id check is optional (empty string = wildcard).
    """
    now = time.time()
    with _STORE_LOCK:
        tok = _STORE.get(token_id)
        if tok is None:
            logger.warning("step_up_consume_failed: token not found token_id=%s", token_id)
            return False
        if tok.used:
            logger.warning("step_up_consume_failed: already used token_id=%s", token_id)
            return False
        if tok.expires_at < now:
            logger.warning("step_up_consume_failed: expired token_id=%s", token_id)
            del _STORE[token_id]
            return False
        if tok.actor != actor:
            logger.warning(
                "step_up_consume_failed: actor mismatch expected=%s got=%s",
                tok.actor, actor,
            )
            return False
        if tok.action_class != action_class:
            logger.warning(
                "step_up_consume_failed: action_class mismatch expected=%s got=%s",
                tok.action_class.value, action_class.value,
            )
            return False
        if resource_id and tok.resource_id and tok.resource_id != resource_id:
            logger.warning(
                "step_up_consume_failed: resource_id mismatch expected=%s got=%s",
                tok.resource_id, resource_id,
            )
            return False
        tok.used = True
    emit_audit_event(
        actor=actor,
        action="step_up_consumed",
        resource_type="step_up_token",
        resource_id=token_id,
        after={
            "action_class": action_class.value,
            "resource_id": resource_id,
        },
    )
    logger.info(
        "Step-up token consumed actor=%s action_class=%s token_id=%s",
        actor, action_class.value, token_id,
    )
    return True


def approve_step_up_token(
    *,
    token_id: str,
    approver: str,
) -> bool:
    """Maker-checker: a second admin approves a pending step-up token."""
    with _STORE_LOCK:
        tok = _STORE.get(token_id)
        if tok is None or tok.used or tok.expires_at < time.time():
            return False
        if tok.actor == approver:
            # Cannot self-approve
            logger.warning("step_up_self_approve_rejected actor=%s", approver)
            return False
        tok.approver = approver
    emit_audit_event(
        actor=approver,
        action="step_up_approved",
        resource_type="step_up_token",
        resource_id=token_id,
        after={
            "approved_for": tok.actor,
            "action_class": tok.action_class.value,
        },
    )
    return True


def purge_expired() -> int:
    """Remove expired tokens. Returns count removed."""
    now = time.time()
    with _STORE_LOCK:
        expired = [k for k, v in _STORE.items() if v.expires_at < now]
        for k in expired:
            del _STORE[k]
    return len(expired)


def pending_approvals(actor: Optional[str] = None) -> list[dict[str, Any]]:
    """Return unexpired, unused, unapproved step-up tokens (for the approval UI)."""
    now = time.time()
    with _STORE_LOCK:
        tokens = list(_STORE.values())
    result = []
    for tok in tokens:
        if tok.used or tok.expires_at < now:
            continue
        if actor and tok.actor != actor:
            continue
        result.append({
            "token_id": tok.token_id,
            "actor": tok.actor,
            "action_class": tok.action_class.value,
            "action_label": _ACTION_CLASS_LABELS.get(tok.action_class, tok.action_class.value),
            "resource_id": tok.resource_id,
            "issued_at": tok.issued_at,
            "expires_at": tok.expires_at,
            "approver": tok.approver,
        })
    return sorted(result, key=lambda x: x["issued_at"], reverse=True)


__all__ = [
    "DangerousActionClass",
    "StepUpToken",
    "approve_step_up_token",
    "consume_step_up_token",
    "issue_step_up_token",
    "pending_approvals",
    "purge_expired",
]
