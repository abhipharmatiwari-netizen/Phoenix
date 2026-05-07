"""Step-up approval gate for dangerous/privileged actions — PHX-SEC-006.

Dangerous actions (kill-switch clear, strategy enable/disable, capital-limit
changes, break-glass) require either:
  1. A time-limited step-up token issued by re-authentication, OR
  2. A maker-checker approval record signed by a second admin.

Step-up tokens are short-lived (5 minutes default), single-use, and bound to
a specific action class.  In LIVE mode they are persisted to Postgres (migration
010) so pending approvals survive a container restart. (#110)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
# Step-up token dataclass
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


# ---------------------------------------------------------------------------
# In-memory store (fast path) + optional Postgres persistence (#110)
# ---------------------------------------------------------------------------

_STORE: dict[str, StepUpToken] = {}
_STORE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Postgres persistence helpers (#110)
# ---------------------------------------------------------------------------

def _is_live_mode() -> bool:
    return str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE"


def _persist_issued(tok: StepUpToken) -> None:
    """Write a newly issued token to Postgres (LIVE only)."""
    if not _is_live_mode():
        return
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO step_up_tokens
                        (token_id, actor, action_class, resource_id,
                         issued_at, expires_at, used)
                    VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                    ON CONFLICT (token_id) DO NOTHING
                    """,
                    (
                        tok.token_id,
                        tok.actor,
                        tok.action_class.value,
                        tok.resource_id,
                        datetime.fromtimestamp(tok.issued_at, tz=timezone.utc),
                        datetime.fromtimestamp(tok.expires_at, tz=timezone.utc),
                    ),
                )
    except Exception as exc:
        # In LIVE, failure to persist is a hard error — caller must not issue
        # an in-memory-only token when durable storage is required.
        raise RuntimeError(
            f"step_up: Postgres persistence failed in LIVE mode. "
            f"Cannot issue token without durable storage: {exc}"
        ) from exc


def _persist_consumed(token_id: str) -> None:
    """Mark a token used=TRUE in Postgres (LIVE only)."""
    if not _is_live_mode():
        return
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE step_up_tokens SET used = TRUE, used_at = NOW() "
                    "WHERE token_id = %s",
                    (token_id,),
                )
    except Exception as exc:
        logger.error("step_up: failed to mark token consumed in Postgres: %s", exc)


def _persist_approved(token_id: str, approver: str) -> None:
    """Record the approver identity in Postgres (LIVE only)."""
    if not _is_live_mode():
        return
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE step_up_tokens SET approver = %s, approved_at = NOW() "
                    "WHERE token_id = %s AND used = FALSE",
                    (approver, token_id),
                )
    except Exception as exc:
        logger.error("step_up: failed to record approver in Postgres: %s", exc)


def load_from_postgres() -> int:
    """On startup, restore unexpired unused tokens from Postgres into the in-memory store.

    Also prunes tokens expired for more than 24 hours to prevent table growth.
    Returns count of tokens restored. (#110)
    """
    if not _is_live_mode():
        return 0
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                # Prune tokens expired > 24 hours ago
                cur.execute(
                    "DELETE FROM step_up_tokens "
                    "WHERE expires_at < NOW() - INTERVAL '24 hours'"
                )
                # Load unexpired, unused tokens
                cur.execute(
                    "SELECT token_id, actor, action_class, resource_id, "
                    "       EXTRACT(EPOCH FROM issued_at), "
                    "       EXTRACT(EPOCH FROM expires_at), "
                    "       used, approver "
                    "FROM step_up_tokens "
                    "WHERE expires_at > NOW() AND used = FALSE"
                )
                rows = cur.fetchall()
        count = 0
        with _STORE_LOCK:
            for row in rows:
                (tid, actor, action_class_val, resource_id,
                 issued_ts, expires_ts, used, approver) = row
                try:
                    action_class = DangerousActionClass(action_class_val)
                except ValueError:
                    logger.warning(
                        "step_up.load: unknown action_class %r for token %s — skipped",
                        action_class_val, tid,
                    )
                    continue
                tok = StepUpToken(
                    token_id=tid,
                    actor=actor,
                    action_class=action_class,
                    resource_id=resource_id or "",
                    issued_at=float(issued_ts),
                    expires_at=float(expires_ts),
                    used=bool(used),
                    approver=approver,
                )
                _STORE[tid] = tok
                count += 1
        logger.info(
            "step_up.load_from_postgres: restored %d unexpired token(s) from Postgres",
            count,
        )
        return count
    except Exception as exc:
        logger.warning("step_up.load_from_postgres failed (non-fatal): %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def issue_step_up_token(
    *,
    actor: str,
    action_class: DangerousActionClass,
    resource_id: str = "",
    ttl_seconds: int = _STEP_UP_TTL_SECONDS,
) -> StepUpToken:
    """Issue a step-up token for an actor/action.

    In LIVE mode, persists to Postgres before returning. Raises RuntimeError if
    Postgres is unavailable in LIVE (fail closed — in-memory-only tokens are not
    permitted for LIVE dangerous actions). (#110)
    """
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
    # Persist to Postgres BEFORE writing to in-memory store so that a failure
    # does not leave a token in memory that is not durable. (#110)
    _persist_issued(tok)
    with _STORE_LOCK:
        _STORE[token_id] = tok
    emit_audit_event(
        actor=actor,
        action="step_up_issued",
        resource_type="step_up_token",
        resource_id=token_id,
        metadata={
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
    # Persist consumed state to Postgres (best-effort; already marked in-memory). (#110)
    _persist_consumed(token_id)
    emit_audit_event(
        actor=actor,
        action="step_up_consumed",
        resource_type="step_up_token",
        resource_id=token_id,
        metadata={
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
            logger.warning("step_up_self_approve_rejected actor=%s", approver)
            return False
        tok.approver = approver
    _persist_approved(token_id, approver)  # (#110)
    emit_audit_event(
        actor=approver,
        action="step_up_approved",
        resource_type="step_up_token",
        resource_id=token_id,
        metadata={
            "approved_for": tok.actor,
            "action_class": tok.action_class.value,
        },
    )
    return True


def purge_expired() -> int:
    """Remove expired tokens from in-memory store. Returns count removed."""
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
    "load_from_postgres",
    "pending_approvals",
    "purge_expired",
]
