"""
Structured audit logging for state-changing admin operations.

Every mutation through admin/control-tower APIs emits an audit record
with actor, action, target, before/after state, and timestamp.
Records are persisted to a local JSONL file so they remain queryable
without requiring a separate database in Sprint 1.
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


__all__ = ["emit_audit_event", "list_audit_events"]
