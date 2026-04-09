"""SLO definitions and error budget tracker — PHX-SRE-003.

Defines SLOs for:
  - Data freshness (quote age < 30s)
  - Decision latency (intent-to-ack < 2s p95)
  - Order acknowledgement (ack within 5s p99)
  - Reconciliation freshness (last successful recon < 10 min)
  - Alert response time (alert-to-action < 5 min)

Error budget = 1 - SLO target
  e.g. 99.9% SLO → 0.1% error budget → ~43.8 min/month

The tracker maintains a rolling window of SLI measurements and
computes the current error budget remaining.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 30 * 24 * 60 * 60  # 30-day rolling window


@dataclass
class SLODefinition:
    name: str
    description: str
    target: float           # e.g. 0.999 = 99.9%
    window_seconds: int = _WINDOW_SECONDS
    # Unit for display
    unit: str = "ratio"     # "ratio", "seconds", "count"


@dataclass
class SLIMeasurement:
    timestamp: float
    good: bool              # True = SLI met, False = SLI violated
    value: Optional[float] = None   # the measured value (latency, age, etc.)


@dataclass
class SLOStatus:
    name: str
    target: float
    sli_ratio: float        # actual good/total over window
    error_budget_remaining: float  # fraction of error budget remaining (0.0–1.0)
    error_budget_minutes: float    # minutes of error budget left in window
    good_count: int
    total_count: int
    is_breached: bool
    window_seconds: int


class SLOTracker:
    """Tracks SLI measurements and computes error budget status."""

    def __init__(self, definition: SLODefinition) -> None:
        self.definition = definition
        self._lock = threading.Lock()
        self._measurements: deque[SLIMeasurement] = deque()

    def record(self, good: bool, value: Optional[float] = None) -> None:
        now = time.time()
        m = SLIMeasurement(timestamp=now, good=good, value=value)
        with self._lock:
            self._measurements.append(m)
            # Evict old measurements
            cutoff = now - self.definition.window_seconds
            while self._measurements and self._measurements[0].timestamp < cutoff:
                self._measurements.popleft()

    def status(self) -> SLOStatus:
        now = time.time()
        cutoff = now - self.definition.window_seconds
        with self._lock:
            recent = [m for m in self._measurements if m.timestamp >= cutoff]

        total = len(recent)
        good = sum(1 for m in recent if m.good)
        sli = good / total if total > 0 else 1.0  # assume good if no data

        target = self.definition.target
        error_budget_total = 1.0 - target
        error_budget_used = max(0.0, target - sli) if sli < target else 0.0
        error_budget_remaining = max(0.0, 1.0 - (error_budget_used / error_budget_total)) if error_budget_total > 0 else 1.0

        window_minutes = self.definition.window_seconds / 60
        error_budget_minutes = error_budget_total * window_minutes * error_budget_remaining

        return SLOStatus(
            name=self.definition.name,
            target=target,
            sli_ratio=round(sli, 6),
            error_budget_remaining=round(error_budget_remaining, 4),
            error_budget_minutes=round(error_budget_minutes, 1),
            good_count=good,
            total_count=total,
            is_breached=sli < target and total > 10,
            window_seconds=self.definition.window_seconds,
        )


class SLORegistry:
    """Registry of all SLO trackers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trackers: dict[str, SLOTracker] = {}

    def register(self, definition: SLODefinition) -> SLOTracker:
        tracker = SLOTracker(definition)
        with self._lock:
            self._trackers[definition.name] = tracker
        return tracker

    def get(self, name: str) -> Optional[SLOTracker]:
        with self._lock:
            return self._trackers.get(name)

    def record(self, name: str, good: bool, value: Optional[float] = None) -> None:
        tracker = self.get(name)
        if tracker:
            tracker.record(good, value)

    def all_status(self) -> list[dict[str, Any]]:
        with self._lock:
            trackers = list(self._trackers.values())
        result = []
        for t in trackers:
            s = t.status()
            result.append({
                "name": s.name,
                "target_pct": round(s.target * 100, 3),
                "sli_pct": round(s.sli_ratio * 100, 3),
                "error_budget_remaining_pct": round(s.error_budget_remaining * 100, 1),
                "error_budget_minutes": s.error_budget_minutes,
                "good": s.good_count,
                "total": s.total_count,
                "is_breached": s.is_breached,
            })
        return result

    def breached_slos(self) -> list[str]:
        with self._lock:
            trackers = list(self._trackers.values())
        return [t.definition.name for t in trackers if t.status().is_breached]


# ---------------------------------------------------------------------------
# Pre-defined Phoenix SLOs
# ---------------------------------------------------------------------------

slo_registry = SLORegistry()

# Data freshness: quotes must be < 30s old
slo_data_freshness = slo_registry.register(SLODefinition(
    name="data_freshness",
    description="Quote age must be < 30s",
    target=0.999,
    unit="seconds",
))

# Decision latency: intent-to-ack < 2s (p95 target)
slo_decision_latency = slo_registry.register(SLODefinition(
    name="decision_latency",
    description="Order intent-to-broker-ack < 2s",
    target=0.95,
    unit="seconds",
))

# Order ack: order acknowledged within 5s (p99)
slo_order_ack = slo_registry.register(SLODefinition(
    name="order_ack",
    description="Order acknowledged within 5s",
    target=0.99,
    unit="seconds",
))

# Reconciliation freshness: last successful recon < 10 min
slo_reconciliation_freshness = slo_registry.register(SLODefinition(
    name="reconciliation_freshness",
    description="Reconciliation completed within last 10 minutes",
    target=0.99,
    unit="boolean",
))

# Alert response: alert-to-action (mitigation) < 5 min
slo_alert_response = slo_registry.register(SLODefinition(
    name="alert_response",
    description="Alert acknowledged/mitigated within 5 minutes",
    target=0.95,
    unit="minutes",
))


__all__ = [
    "SLODefinition",
    "SLORegistry",
    "SLOStatus",
    "SLOTracker",
    "slo_alert_response",
    "slo_data_freshness",
    "slo_decision_latency",
    "slo_order_ack",
    "slo_reconciliation_freshness",
    "slo_registry",
]
