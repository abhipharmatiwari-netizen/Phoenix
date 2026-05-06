"""Prometheus metrics for Architecture S15.2 observability and SLOs.

Defines the required metrics:
- phoenix_order_intent_to_ack_seconds (histogram)
- phoenix_order_terminal_convergence_seconds (histogram)
- phoenix_reconciliation_backlog_age_seconds (gauge)
- phoenix_outbox_recovery_duration_seconds (histogram)
- phoenix_broker_sync_latency_seconds (histogram per account)
- phoenix_quote_freshness_seconds (gauge per instrument class)

These are implemented as lightweight in-process collectors that can be
exported to Prometheus via a /metrics endpoint or polled by a scraper.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Histogram implementation (lightweight, no prometheus_client dependency)
# ---------------------------------------------------------------------------

class Histogram:
    """Simple histogram with configurable buckets."""

    DEFAULT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, float("inf"))

    def __init__(
        self,
        name: str,
        description: str,
        *,
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        label_names: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self._buckets = tuple(sorted(buckets))
        self._label_names = label_names
        self._lock = threading.Lock()
        # label_key -> {bucket_bound: count, "_sum": float, "_count": int}
        self._data: Dict[tuple, Dict[str, float]] = defaultdict(
            lambda: {str(b): 0.0 for b in self._buckets} | {"_sum": 0.0, "_count": 0.0}
        )

    def observe(self, value: float, *, labels: tuple = ()) -> None:
        with self._lock:
            data = self._data[labels]
            data["_sum"] += value
            data["_count"] += 1
            for b in self._buckets:
                if value <= b:
                    data[str(b)] += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "type": "histogram",
                "data": {
                    str(k): dict(v) for k, v in self._data.items()
                },
            }


class Gauge:
    """Simple gauge metric."""

    def __init__(
        self,
        name: str,
        description: str,
        *,
        label_names: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self._label_names = label_names
        self._lock = threading.Lock()
        self._values: Dict[tuple, float] = defaultdict(float)

    def set(self, value: float, *, labels: tuple = ()) -> None:
        with self._lock:
            self._values[labels] = value

    def inc(self, amount: float = 1.0, *, labels: tuple = ()) -> None:
        with self._lock:
            self._values[labels] += amount

    def get(self, *, labels: tuple = ()) -> float:
        with self._lock:
            return self._values.get(labels, 0.0)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "type": "gauge",
                "data": {str(k): v for k, v in self._values.items()},
            }


# ---------------------------------------------------------------------------
# Phoenix metrics registry
# ---------------------------------------------------------------------------

# Order intent to broker ack latency
order_intent_to_ack_seconds = Histogram(
    "phoenix_order_intent_to_ack_seconds",
    "Time from order intent creation to broker acknowledgement",
    label_names=("tenant_id", "broker_account_id"),
)

# Terminal lifecycle convergence time
order_terminal_convergence_seconds = Histogram(
    "phoenix_order_terminal_convergence_seconds",
    "Time from order creation to terminal state",
    label_names=("tenant_id", "broker_account_id"),
)

# Reconciliation backlog age
reconciliation_backlog_age_seconds = Gauge(
    "phoenix_reconciliation_backlog_age_seconds",
    "Age of the oldest unresolved reconciliation item",
    label_names=("tenant_id", "broker_account_id"),
)

# Outbox recovery duration after restart
outbox_recovery_duration_seconds = Histogram(
    "phoenix_outbox_recovery_duration_seconds",
    "Duration of outbox recovery after restart",
)

# Broker sync latency per account
broker_sync_latency_seconds = Histogram(
    "phoenix_broker_sync_latency_seconds",
    "Broker position/order sync poll latency",
    label_names=("tenant_id", "broker_account_id"),
)

# Quote freshness per instrument class
quote_freshness_seconds = Gauge(
    "phoenix_quote_freshness_seconds",
    "Age of the latest quote per instrument class",
    label_names=("instrument_class",),
)


def collect_all_metrics() -> List[Dict[str, Any]]:
    """Return snapshots of all registered metrics."""
    return [
        order_intent_to_ack_seconds.snapshot(),
        order_terminal_convergence_seconds.snapshot(),
        reconciliation_backlog_age_seconds.snapshot(),
        outbox_recovery_duration_seconds.snapshot(),
        broker_sync_latency_seconds.snapshot(),
        quote_freshness_seconds.snapshot(),
    ]


__all__ = [
    "Gauge",
    "Histogram",
    "broker_sync_latency_seconds",
    "collect_all_metrics",
    "order_intent_to_ack_seconds",
    "order_terminal_convergence_seconds",
    "outbox_recovery_duration_seconds",
    "quote_freshness_seconds",
    "reconciliation_backlog_age_seconds",
]
