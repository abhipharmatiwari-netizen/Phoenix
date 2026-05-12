"""
Structured audit logging for state-changing admin operations — PHX-AUD-001.

Every mutation through admin/control-tower APIs emits an audit record
with actor, action, target, before/after state, and timestamp.

Persistence strategy (dual-write):
1. Local JSONL file — low-latency, always available, used as fast query path.
2. Postgres audit_events table — durable, append-only, queryable across restarts.
   Rows are INSERT-only; no UPDATE/DELETE on audit rows is permitted.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from uuid import uuid4

from app.core.logging_utils import get_log_context

logger = logging.getLogger("phoenix.audit")

_AUDIT_LOG_ENABLED = True
_AUDIT_FILE_LOCK = Lock()
_RECENT_AUDIT_EVENTS: deque[dict[str, Any]] = deque(maxlen=512)
_DEFAULT_AUDIT_LOG_PATH = (
    Path(__file__).resolve().parents[2] / "logs" / "audit_events.jsonl"
)


def _audit_log_path() -> Path:
    raw_path = str(os.getenv("AUDIT_LOG_PATH", "") or "").strip()
    if not raw_path:
        return _DEFAULT_AUDIT_LOG_PATH
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def _append_audit_event(event: dict[str, Any]) -> None:
    payload = json.dumps(event, default=str)
    path = _audit_log_path()
    with _AUDIT_FILE_LOCK:
        _RECENT_AUDIT_EVENTS.append(dict(event))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    # PHX-AUD-001: dual-write to Postgres (fire-and-forget; never blocks caller)
    _try_postgres_persist(event)


def _audit_postgres_max_attempts() -> int:
    """Per-call ``max_attempts`` override for the audit dual-write.

    PR #258 round-5 P2 (Codex line 1845): the kill-switch bridge's
    audit re-emit calls ``emit_audit_event`` synchronously and must
    bound the Postgres dual-write's wall-clock to fit inside the
    bridge's remaining deadline. The default
    ``connect_with_retry(max_attempts=3)`` plus 0.5/1.0s internal
    backoff can consume up to ``3 * connect_timeout + 1.5s`` before
    returning, blowing the advertised hard deadline. When
    ``AUDIT_POSTGRES_MAX_ATTEMPTS`` is set, this overrides the
    default so the bridge can scope it to ``max_attempts=1`` for the
    duration of its re-emit call (and any other caller wishing to
    bound the dual-write wall-clock can do the same).

    Returns the override on a valid positive int, else the default
    ``3`` so unrelated callers are unaffected.
    """
    raw = os.getenv("AUDIT_POSTGRES_MAX_ATTEMPTS", "").strip()
    if not raw:
        return 3
    try:
        parsed = int(float(raw))
        if parsed <= 0:
            return 3
        return parsed
    except Exception:
        return 3


def _try_postgres_persist(event: dict[str, Any]) -> None:
    """Append-only insert to Postgres audit_events table.  Never raises."""
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(
            dsn,
            autocommit=True,
            max_attempts=_audit_postgres_max_attempts(),
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_events
                        (audit_id, timestamp, actor, action, resource_type,
                         resource_id, request_id, idempotency_key,
                         before_state, after_state, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (audit_id) DO NOTHING
                    """,
                    (
                        event.get("audit_id"),
                        event.get("timestamp"),
                        event.get("actor"),
                        event.get("action"),
                        event.get("resource_type"),
                        event.get("resource_id"),
                        event.get("request_id") or None,
                        event.get("idempotency_key") or None,
                        json.dumps(event.get("before"), default=str) if event.get("before") is not None else None,
                        json.dumps(event.get("after"), default=str) if event.get("after") is not None else None,
                        json.dumps(event.get("metadata"), default=str) if event.get("metadata") else None,
                    ),
                )
    except Exception:
        logger.debug("Postgres audit persist unavailable — JSONL only", exc_info=True)


def list_audit_events(
    *,
    limit: int = 100,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return most recent audit events, newest first."""
    effective_limit = max(1, min(int(limit), 500))
    actor_filter = str(actor or "").strip()
    action_filter = str(action or "").strip()
    resource_type_filter = str(resource_type or "").strip()
    resource_id_filter = str(resource_id or "").strip()
    path = _audit_log_path()
    with _AUDIT_FILE_LOCK:
        if path.is_file():
            events: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        events.append(payload)
        else:
            events = [dict(item) for item in _RECENT_AUDIT_EVENTS]
    filtered = [
        event
        for event in events
        if (
            (not actor_filter or str(event.get("actor", "")) == actor_filter)
            and (not action_filter or str(event.get("action", "")) == action_filter)
            and (
                not resource_type_filter
                or str(event.get("resource_type", "")) == resource_type_filter
            )
            and (
                not resource_id_filter
                or str(event.get("resource_id", "")) == resource_id_filter
            )
        )
    ]
    return list(reversed(filtered[-effective_limit:]))


def emit_audit_event(
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    before: Optional[Any] = None,
    after: Optional[Any] = None,
    request_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Emit a structured audit event and return the record."""
    if not _AUDIT_LOG_ENABLED:
        return {}

    correlation_id, ctx_request_id = get_log_context()
    resolved_request_id = str(request_id or ctx_request_id or "").strip()
    resolved_metadata = dict(metadata or {})
    if correlation_id and "correlation_id" not in resolved_metadata:
        resolved_metadata["correlation_id"] = correlation_id
    resolved_idempotency_key = str(
        idempotency_key or resolved_metadata.get("idempotency_key") or ""
    ).strip()

    event = {
        "audit_id": uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "request_id": resolved_request_id,
    }
    if resolved_idempotency_key:
        event["idempotency_key"] = resolved_idempotency_key
    if before is not None:
        event["before"] = _safe_serialize(before)
    if after is not None:
        event["after"] = _safe_serialize(after)
    if resolved_metadata:
        event["metadata"] = _safe_serialize(resolved_metadata)

    _append_audit_event(event)
    logger.info("AUDIT %s", json.dumps(event, default=str))
    return event


def _safe_serialize(value: Any) -> Any:
    """Convert to JSON-safe representation."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _safe_serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_serialize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _safe_serialize(asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _safe_serialize(value.model_dump())
    if hasattr(value, "__dict__"):
        return {
            k: _safe_serialize(v)
            for k, v in value.__dict__.items()
            if not k.startswith("_")
        }
    return str(value)


def purge_old_audit_events(*, retain_days: int = 90) -> int:
    """PHX-AUD-004: Delete audit events older than retain_days from Postgres.

    The JSONL file is NOT truncated — it serves as the immutable evidence archive.
    Returns the number of rows deleted from Postgres.
    """
    deleted = 0
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM audit_events
                    WHERE timestamp < NOW() - INTERVAL '%s days'
                    """,
                    (max(1, int(retain_days)),),
                )
                deleted = cur.rowcount
        logger.info("audit_retention_purge deleted=%d retain_days=%d", deleted, retain_days)
    except Exception:
        logger.debug("Postgres audit retention purge unavailable", exc_info=True)
    return deleted


def accountability_report(
    *,
    since_days: int = 7,
    actor: Optional[str] = None,
) -> dict[str, Any]:
    """PHX-AUD-005: Generate an accountability summary for the given window.

    Returns counts of overrides, kill-switch events, privilege changes, and
    break-glass events grouped by actor.
    """
    dangerous_actions = {
        "kill_switch_clear", "kill_switch_rearm", "kill_switch_trip",
        # PR #240 round-6 review P2 (issue #238): the dashboard
        # bulk-cancel path is just as destructive as the kill-switch
        # state transitions — incident reviewers must see it in the
        # accountability summary too. Also covers the compensating
        # rollback audit and the SOFT→HARD upgrade variant.
        "kill_switch_cancel_all",
        "kill_switch_block_exits_upgraded",
        "kill_switch_persist_failed_rollback",
        "promote_user", "step_up_issued", "step_up_consumed", "step_up_approved",
        "break_glass", "capital_limit_change", "strategy_enable", "strategy_disable",
        "config_change", "manual_override",
    }
    events = list_audit_events(limit=500, actor=actor)
    # Filter by window
    cutoff = datetime.now(timezone.utc).timestamp() - (since_days * 86400)
    summary: dict[str, dict[str, int]] = {}
    for evt in events:
        ts = evt.get("timestamp", "")
        try:
            from datetime import datetime as _dt
            ts_val = _dt.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts_val < cutoff:
            continue
        a = str(evt.get("actor") or "unknown")
        action = str(evt.get("action") or "")
        if a not in summary:
            summary[a] = {k: 0 for k in dangerous_actions} | {"_total": 0}
        summary[a]["_total"] += 1
        if action in dangerous_actions:
            summary[a][action] = summary[a].get(action, 0) + 1

    return {
        "since_days": since_days,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "actors": summary,
        "total_events": sum(v["_total"] for v in summary.values()),
    }


__all__ = [
    "accountability_report",
    "emit_audit_event",
    "list_audit_events",
    "purge_old_audit_events",
]
