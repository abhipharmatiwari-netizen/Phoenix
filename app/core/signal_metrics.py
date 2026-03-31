"""Thread-safe counters for periodic signal activity summaries."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class SignalSummarySnapshot:
    uptime_seconds: float
    window_seconds: float
    window_signals_checked: int
    window_signals_fired: int
    window_orders_submitted: int
    total_signals_checked: int
    total_signals_fired: int
    total_orders_submitted: int


@dataclass(frozen=True)
class LabeledMetricPoint:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float


class SignalSummaryMetrics:
    """Tracks strategy checks, fired signals, and submitted orders."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        now = time.monotonic()
        self._started_mono = now
        self._last_emit_mono = now
        self._window_signals_checked = 0
        self._window_signals_fired = 0
        self._window_orders_submitted = 0
        self._total_signals_checked = 0
        self._total_signals_fired = 0
        self._total_orders_submitted = 0

    def mark_signal_checked(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._window_signals_checked += count
            self._total_signals_checked += count

    def mark_signal_fired(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._window_signals_fired += count
            self._total_signals_fired += count

    def mark_order_submitted(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._window_orders_submitted += count
            self._total_orders_submitted += count

    def snapshot(self) -> SignalSummarySnapshot:
        now = time.monotonic()
        with self._lock:
            return SignalSummarySnapshot(
                uptime_seconds=max(0.0, now - self._started_mono),
                window_seconds=max(0.0, now - self._last_emit_mono),
                window_signals_checked=self._window_signals_checked,
                window_signals_fired=self._window_signals_fired,
                window_orders_submitted=self._window_orders_submitted,
                total_signals_checked=self._total_signals_checked,
                total_signals_fired=self._total_signals_fired,
                total_orders_submitted=self._total_orders_submitted,
            )

    def maybe_log_summary(
        self,
        logger: logging.Logger,
        interval_seconds: float,
        *,
        force: bool = False,
    ) -> Optional[SignalSummarySnapshot]:
        try:
            interval = float(interval_seconds)
        except (TypeError, ValueError):
            interval = 60.0
        interval = max(1.0, interval)

        now = time.monotonic()
        with self._lock:
            window_elapsed = now - self._last_emit_mono
            if not force and window_elapsed < interval:
                return None
            snapshot = SignalSummarySnapshot(
                uptime_seconds=max(0.0, now - self._started_mono),
                window_seconds=max(0.0, window_elapsed),
                window_signals_checked=self._window_signals_checked,
                window_signals_fired=self._window_signals_fired,
                window_orders_submitted=self._window_orders_submitted,
                total_signals_checked=self._total_signals_checked,
                total_signals_fired=self._total_signals_fired,
                total_orders_submitted=self._total_orders_submitted,
            )
            self._window_signals_checked = 0
            self._window_signals_fired = 0
            self._window_orders_submitted = 0
            self._last_emit_mono = now

        logger.info(
            "[SIGNAL_SUMMARY] window=%.1fs checked=%d fired=%d orders_submitted=%d "
            "totals_checked=%d totals_fired=%d totals_orders_submitted=%d uptime=%.1fs",
            snapshot.window_seconds,
            snapshot.window_signals_checked,
            snapshot.window_signals_fired,
            snapshot.window_orders_submitted,
            snapshot.total_signals_checked,
            snapshot.total_signals_fired,
            snapshot.total_orders_submitted,
            snapshot.uptime_seconds,
        )
        return snapshot

    def reset_for_tests(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._started_mono = now
            self._last_emit_mono = now
            self._window_signals_checked = 0
            self._window_signals_fired = 0
            self._window_orders_submitted = 0
            self._total_signals_checked = 0
            self._total_signals_fired = 0
            self._total_orders_submitted = 0


class LabeledMetricsRegistry:
    """Thread-safe in-process labeled metric store for counters/gauges."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    @staticmethod
    def _normalize_labels(labels: Mapping[str, object] | None) -> tuple[tuple[str, str], ...]:
        if not labels:
            return tuple()
        out: list[tuple[str, str]] = []
        for key, value in labels.items():
            key_txt = str(key).strip()
            if not key_txt:
                continue
            out.append((key_txt, str(value)))
        out.sort(key=lambda kv: kv[0])
        return tuple(out)

    def inc_counter(
        self,
        name: str,
        *,
        labels: Mapping[str, object] | None = None,
        amount: float = 1.0,
    ) -> None:
        if amount <= 0:
            return
        key = (str(name), self._normalize_labels(labels))
        with self._lock:
            self._counters[key] = float(self._counters.get(key, 0.0)) + float(amount)

    def set_gauge(
        self,
        name: str,
        *,
        labels: Mapping[str, object] | None = None,
        value: float,
    ) -> None:
        key = (str(name), self._normalize_labels(labels))
        with self._lock:
            self._gauges[key] = float(value)

    def counter_value(self, name: str, *, labels: Mapping[str, object] | None = None) -> float:
        key = (str(name), self._normalize_labels(labels))
        with self._lock:
            return float(self._counters.get(key, 0.0))

    def gauge_value(self, name: str, *, labels: Mapping[str, object] | None = None) -> float:
        key = (str(name), self._normalize_labels(labels))
        with self._lock:
            return float(self._gauges.get(key, 0.0))

    def snapshot_counters(self) -> list[LabeledMetricPoint]:
        with self._lock:
            return [
                LabeledMetricPoint(name=name, labels=labels, value=value)
                for (name, labels), value in sorted(self._counters.items(), key=lambda kv: kv[0])
            ]

    def snapshot_gauges(self) -> list[LabeledMetricPoint]:
        with self._lock:
            return [
                LabeledMetricPoint(name=name, labels=labels, value=value)
                for (name, labels), value in sorted(self._gauges.items(), key=lambda kv: kv[0])
            ]

    def reset_for_tests(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


NON_SUBMITTED_STATUSES = {
    "",
    "REJECTED",
    "FAILED",
    "ERROR",
    "NOT_PLACED",
    "NOTPLACED",
    "BLOCKED",
}


def is_submitted_order_response(*, status: object, broker_order_id: object) -> bool:
    if str(broker_order_id or "").strip():
        return True
    status_upper = str(status or "").strip().upper()
    return status_upper not in NON_SUBMITTED_STATUSES


_GLOBAL_SIGNAL_SUMMARY_METRICS = SignalSummaryMetrics()
_GLOBAL_LABELED_METRICS = LabeledMetricsRegistry()


def get_signal_summary_metrics() -> SignalSummaryMetrics:
    return _GLOBAL_SIGNAL_SUMMARY_METRICS


def get_labeled_metrics() -> LabeledMetricsRegistry:
    return _GLOBAL_LABELED_METRICS


__all__ = [
    "SignalSummarySnapshot",
    "SignalSummaryMetrics",
    "LabeledMetricPoint",
    "LabeledMetricsRegistry",
    "NON_SUBMITTED_STATUSES",
    "is_submitted_order_response",
    "get_signal_summary_metrics",
    "get_labeled_metrics",
]
