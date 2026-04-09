"""Incident tooling — PHX-SRE-005.

Provides:
  - One-click incident snapshot: captures all relevant state at declaration time
  - Incident lifecycle: OPEN → INVESTIGATING → MITIGATED → RESOLVED
  - Postmortem template generation
  - Incident action tracking

An incident snapshot includes:
  - Active alerts
  - Degraded scopes
  - Kill-switch state
  - Recent audit events
  - SLO status
  - Active orders (open/pending)
  - System health metrics
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


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"


class IncidentSeverity(str, Enum):
    SEV1 = "SEV1"   # Trading halted / money at risk
    SEV2 = "SEV2"   # Degraded trading / significant data quality issue
    SEV3 = "SEV3"   # Minor degradation / latency
    SEV4 = "SEV4"   # Informational / no user impact


@dataclass
class IncidentAction:
    actor: str
    action: str
    note: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )


@dataclass
class Incident:
    incident_id: str
    title: str
    severity: IncidentSeverity
    declared_by: str
    status: IncidentStatus = IncidentStatus.OPEN
    summary: str = ""
    snapshot: dict = field(default_factory=dict)
    actions: list[IncidentAction] = field(default_factory=list)
    declared_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    resolved_at: Optional[str] = None
    postmortem: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "severity": self.severity.value,
            "declared_by": self.declared_by,
            "status": self.status.value,
            "summary": self.summary,
            "snapshot": self.snapshot,
            "actions": [
                {"actor": a.actor, "action": a.action, "note": a.note, "timestamp": a.timestamp}
                for a in self.actions
            ],
            "declared_at": self.declared_at,
            "resolved_at": self.resolved_at,
        }


_INCIDENTS: dict[str, Incident] = {}
_LOCK = threading.Lock()


def _capture_snapshot() -> dict[str, Any]:
    """Capture a point-in-time snapshot of all relevant system state."""
    snapshot: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    # Active alerts
    try:
        from app.observability.alert_rules import _DEFAULT_EVALUATOR  # type: ignore
        snapshot["active_alerts"] = [
            s.to_dict() for s in _DEFAULT_EVALUATOR._statuses.values()  # type: ignore
            if s.state.value == "firing"  # type: ignore
        ]
    except Exception:
        snapshot["active_alerts"] = []

    # Degraded scopes
    try:
        from app.core.degraded_scope_manager import degraded_scope_manager
        snapshot["degraded_scopes"] = degraded_scope_manager.status_snapshot()
    except Exception:
        snapshot["degraded_scopes"] = {}

    # SLO status
    try:
        from app.observability.slo import slo_registry
        snapshot["slo_status"] = slo_registry.all_status()
        snapshot["breached_slos"] = slo_registry.breached_slos()
    except Exception:
        snapshot["slo_status"] = []

    # Recent audit events (last 20)
    try:
        from app.core.audit_log import list_audit_events
        snapshot["recent_audit_events"] = list_audit_events(limit=20)
    except Exception:
        snapshot["recent_audit_events"] = []

    # Kill-switch state
    try:
        from app.risk.kill_switch import get_kill_switch_state  # type: ignore
        snapshot["kill_switch"] = get_kill_switch_state()
    except Exception:
        snapshot["kill_switch"] = "unknown"

    # Stale quote guard
    try:
        from app.risk.stale_quote_guard import stale_quote_guard
        snapshot["stale_quote_guard"] = stale_quote_guard.status_snapshot()
    except Exception:
        snapshot["stale_quote_guard"] = {}

    return snapshot


def declare_incident(
    *,
    title: str,
    severity: IncidentSeverity,
    declared_by: str,
    summary: str = "",
) -> Incident:
    """Declare a new incident and capture a system snapshot."""
    incident_id = uuid4().hex
    snapshot = _capture_snapshot()
    incident = Incident(
        incident_id=incident_id,
        title=title,
        severity=severity,
        declared_by=declared_by,
        summary=summary,
        snapshot=snapshot,
    )
    incident.actions.append(IncidentAction(
        actor=declared_by,
        action="declare",
        note=f"Incident declared: {title}",
    ))
    with _LOCK:
        _INCIDENTS[incident_id] = incident

    emit_audit_event(
        actor=declared_by,
        action="incident_declared",
        resource_type="incident",
        resource_id=incident_id,
        after={
            "title": title,
            "severity": severity.value,
            "summary": summary,
        },
    )
    logger.warning(
        "incident_declared id=%s severity=%s title=%s by=%s",
        incident_id, severity.value, title, declared_by,
    )
    return incident


def update_incident_status(
    *,
    incident_id: str,
    new_status: IncidentStatus,
    actor: str,
    note: str = "",
) -> tuple[bool, str]:
    with _LOCK:
        incident = _INCIDENTS.get(incident_id)
        if incident is None:
            return False, "Incident not found"
        prev_status = incident.status
        incident.status = new_status
        if new_status == IncidentStatus.RESOLVED:
            incident.resolved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        incident.actions.append(IncidentAction(
            actor=actor,
            action=f"status_change_{new_status.value.lower()}",
            note=note or f"Status changed to {new_status.value}",
        ))

    emit_audit_event(
        actor=actor,
        action="incident_status_changed",
        resource_type="incident",
        resource_id=incident_id,
        before={"status": prev_status.value},
        after={"status": new_status.value},
    )
    return True, f"Status updated to {new_status.value}"


def add_incident_action(
    *,
    incident_id: str,
    actor: str,
    action: str,
    note: str = "",
) -> bool:
    with _LOCK:
        incident = _INCIDENTS.get(incident_id)
        if incident is None:
            return False
        incident.actions.append(IncidentAction(actor=actor, action=action, note=note))
    return True


def generate_postmortem(incident_id: str) -> Optional[str]:
    """Generate a structured postmortem template for an incident."""
    with _LOCK:
        incident = _INCIDENTS.get(incident_id)
    if incident is None:
        return None

    actions_text = "\n".join(
        f"  - [{a.timestamp}] {a.actor}: {a.action} — {a.note}"
        for a in incident.actions
    )
    alerts = incident.snapshot.get("active_alerts", [])
    breached = incident.snapshot.get("breached_slos", [])

    template = f"""# Incident Postmortem: {incident.title}

**ID:** {incident.incident_id}
**Severity:** {incident.severity.value}
**Declared:** {incident.declared_at}
**Resolved:** {incident.resolved_at or 'Not yet resolved'}
**Declared by:** {incident.declared_by}

---

## Summary

{incident.summary or '(fill in summary)'}

---

## Timeline

{actions_text}

---

## Impact

- Active alerts at declaration: {len(alerts)}
- Breached SLOs at declaration: {', '.join(breached) or 'None'}
- Degraded scopes at declaration: {incident.snapshot.get('degraded_scopes', {}).get('degraded_count', 0)}

---

## Root Cause

(fill in root cause analysis)

---

## Contributing Factors

- (list contributing factors)

---

## Resolution

(describe how the incident was resolved)

---

## Action Items

| Action | Owner | Due Date | Priority |
|--------|-------|----------|----------|
| (action 1) | (owner) | (date) | P0 |

---

## Lessons Learned

(what did we learn? what can we improve?)

---

## Metrics

- Time to detect: (fill in)
- Time to mitigate: (fill in)
- Time to resolve: (fill in)
- Impact duration: (fill in)

---
*Generated by Phoenix Incident Tooling — PHX-SRE-005*
"""
    with _LOCK:
        incident.postmortem = template
    return template


def list_incidents(
    status: Optional[IncidentStatus] = None,
    severity: Optional[IncidentSeverity] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with _LOCK:
        incidents = list(_INCIDENTS.values())
    filtered = [
        i for i in incidents
        if (status is None or i.status == status)
        and (severity is None or i.severity == severity)
    ]
    filtered.sort(key=lambda i: i.declared_at, reverse=True)
    return [i.to_dict() for i in filtered[:limit]]


def get_incident(incident_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        i = _INCIDENTS.get(incident_id)
    return i.to_dict() if i else None


__all__ = [
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
    "add_incident_action",
    "declare_incident",
    "generate_postmortem",
    "get_incident",
    "list_incidents",
    "update_incident_status",
]
