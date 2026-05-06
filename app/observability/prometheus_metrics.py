"""
OBS-6.1: Prometheus metrics endpoint with core trading KPIs.

Exposes /metrics in Prometheus text format. Bridges the existing in-memory
LabeledMetricsRegistry and SignalSummaryMetrics to Prometheus counters/gauges.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight Prometheus text-format exporter (no external dependency needed)
# ---------------------------------------------------------------------------

_METRICS_STORE: dict[str, "_MetricEntry"] = {}
_START_TIME = time.monotonic()


class _MetricEntry:
    __slots__ = ("name", "help_text", "metric_type", "samples")

    def __init__(self, name: str, help_text: str, metric_type: str) -> None:
        self.name = name
        self.help_text = help_text
        self.metric_type = metric_type
        # samples: list of (label_dict, value)
        self.samples: list[tuple[dict[str, str], float]] = []


def _label_str(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + parts + "}"


# ---------------------------------------------------------------------------
# Public metric registration / update API
# ---------------------------------------------------------------------------


def counter_inc(
    name: str,
    value: float = 1.0,
    *,
    labels: Optional[dict[str, str]] = None,
    help_text: str = "",
) -> None:
    """Increment a counter metric."""
    key = name
    if key not in _METRICS_STORE:
        _METRICS_STORE[key] = _MetricEntry(name, help_text or name, "counter")
    entry = _METRICS_STORE[key]
    lbl = labels or {}
    for i, (existing_labels, existing_val) in enumerate(entry.samples):
        if existing_labels == lbl:
            entry.samples[i] = (existing_labels, existing_val + value)
            return
    entry.samples.append((lbl, value))


def gauge_set(
    name: str,
    value: float,
    *,
    labels: Optional[dict[str, str]] = None,
    help_text: str = "",
) -> None:
    """Set a gauge metric."""
    key = name
    if key not in _METRICS_STORE:
        _METRICS_STORE[key] = _MetricEntry(name, help_text or name, "gauge")
    entry = _METRICS_STORE[key]
    lbl = labels or {}
    for i, (existing_labels, _) in enumerate(entry.samples):
        if existing_labels == lbl:
            entry.samples[i] = (existing_labels, value)
            return
    entry.samples.append((lbl, value))


def histogram_observe(
    name: str,
    value: float,
    *,
    labels: Optional[dict[str, str]] = None,
    help_text: str = "",
) -> None:
    """Record a histogram observation (simplified: tracks sum and count)."""
    lbl = labels or {}
    counter_inc(f"{name}_count", 1.0, labels=lbl, help_text=f"{help_text} count")
    counter_inc(f"{name}_sum", value, labels=lbl, help_text=f"{help_text} sum")


# ---------------------------------------------------------------------------
# Prometheus text format renderer
# ---------------------------------------------------------------------------


def render_metrics() -> str:
    """Render all metrics in Prometheus exposition text format."""
    lines: list[str] = []

    # Add process uptime
    uptime = time.monotonic() - _START_TIME
    lines.append("# HELP phoenix_uptime_seconds Process uptime in seconds")
    lines.append("# TYPE phoenix_uptime_seconds gauge")
    lines.append(f"phoenix_uptime_seconds {uptime:.2f}")
    lines.append("")

    for entry in _METRICS_STORE.values():
        lines.append(f"# HELP {entry.name} {entry.help_text}")
        lines.append(f"# TYPE {entry.name} {entry.metric_type}")
        for lbl, val in entry.samples:
            label_part = _label_str(lbl)
            lines.append(f"{entry.name}{label_part} {val}")
        lines.append("")

    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    """Clear all metrics (for testing)."""
    _METRICS_STORE.clear()


# ---------------------------------------------------------------------------
# Bridge: sync from existing in-memory registries
# ---------------------------------------------------------------------------


def sync_from_labeled_registry() -> None:
    """Pull metrics from the global LabeledMetricsRegistry into Prometheus format."""
    try:
        from app.core.signal_metrics import get_labeled_metrics, get_signal_summary_metrics

        registry = get_labeled_metrics()

        for point in registry.snapshot_counters():
            lbl = {str(k): str(v) for k, v in (point.labels or {}).items()}
            counter_inc(
                f"phoenix_{point.name}",
                0,  # Don't double-count; set via gauge
                labels=lbl,
                help_text=point.name,
            )
            # Actually use gauge_set to mirror the current value
            gauge_set(f"phoenix_{point.name}_total", point.value, labels=lbl)

        for point in registry.snapshot_gauges():
            lbl = {str(k): str(v) for k, v in (point.labels or {}).items()}
            gauge_set(f"phoenix_{point.name}", point.value, labels=lbl)

        # Signal summary
        summary = get_signal_summary_metrics()
        snap = summary.snapshot()
        if snap:
            gauge_set(
                "phoenix_signals_checked_total",
                float(snap.total_signals_checked),
                help_text="Total signals evaluated",
            )
            gauge_set(
                "phoenix_signals_fired_total",
                float(snap.total_signals_fired),
                help_text="Total signals that triggered orders",
            )
            gauge_set(
                "phoenix_orders_submitted_total",
                float(snap.total_orders_submitted),
                help_text="Total orders submitted to broker",
            )
            gauge_set(
                "phoenix_uptime_app_seconds",
                snap.uptime_seconds,
                help_text="Application uptime from signal metrics",
            )
    except Exception as exc:
        logger.debug("sync_from_labeled_registry error: %s", exc)


# ---------------------------------------------------------------------------
# Pre-defined trading KPI metrics (called from order router, risk, etc.)
# ---------------------------------------------------------------------------

# Order pipeline
def record_order_submitted(
    tenant_id: str, broker_account_id: str, strategy_id: str, status: str
) -> None:
    counter_inc(
        "phoenix_orders_total",
        labels={
            "tenant_id": tenant_id,
            "broker_account_id": broker_account_id,
            "strategy_id": strategy_id,
            "status": status,
        },
        help_text="Total orders processed",
    )


def record_order_latency(latency_seconds: float, strategy_id: str = "") -> None:
    histogram_observe(
        "phoenix_order_latency_seconds",
        latency_seconds,
        labels={"strategy_id": strategy_id} if strategy_id else {},
        help_text="Order submission latency",
    )


# Risk / policy
def record_policy_decision(source: str, action: str) -> None:
    counter_inc(
        "phoenix_policy_decisions_total",
        labels={"source": source, "action": action},
        help_text="Policy interceptor decisions",
    )


def record_circuit_breaker_trip(breaker_type: str) -> None:
    counter_inc(
        "phoenix_circuit_breaker_trips_total",
        labels={"type": breaker_type},
        help_text="Circuit breaker trip events",
    )


# Market data
def record_tick_received(underlying: str) -> None:
    counter_inc(
        "phoenix_ticks_received_total",
        labels={"underlying": underlying},
        help_text="Market data ticks received",
    )


def record_tick_gap_detected(underlying: str) -> None:
    counter_inc(
        "phoenix_tick_gaps_total",
        labels={"underlying": underlying},
        help_text="Market data gap detections",
    )


# Broker sync
def record_position_sync(broker_account_id: str, success: bool) -> None:
    counter_inc(
        "phoenix_position_syncs_total",
        labels={"broker_account_id": broker_account_id, "success": str(success).lower()},
        help_text="Broker position sync attempts",
    )


# PnL
def record_realized_pnl(tenant_id: str, broker_account_id: str, pnl: float) -> None:
    gauge_set(
        "phoenix_realized_pnl",
        pnl,
        labels={"tenant_id": tenant_id, "broker_account_id": broker_account_id},
        help_text="Current realized PnL",
    )


__all__ = [
    "counter_inc",
    "gauge_set",
    "histogram_observe",
    "render_metrics",
    "reset_metrics",
    "sync_from_labeled_registry",
    "record_order_submitted",
    "record_order_latency",
    "record_policy_decision",
    "record_circuit_breaker_trip",
    "record_tick_received",
    "record_tick_gap_detected",
    "record_position_sync",
    "record_realized_pnl",
]
