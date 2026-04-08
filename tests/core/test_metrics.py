"""Tests for Prometheus metrics (M6)."""

from app.core.metrics import (
    Gauge,
    Histogram,
    broker_sync_latency_seconds,
    collect_all_metrics,
    order_intent_to_ack_seconds,
    order_terminal_convergence_seconds,
    quote_freshness_seconds,
    reconciliation_backlog_age_seconds,
)


class TestHistogram:
    def test_observe_increments_buckets(self):
        h = Histogram("test_hist", "test", buckets=(1.0, 5.0, 10.0, float("inf")))
        h.observe(0.5)
        h.observe(3.0)
        h.observe(7.0)
        snap = h.snapshot()
        assert snap["type"] == "histogram"
        data = snap["data"][str(())]
        assert data["_count"] == 3.0
        assert data["_sum"] == 10.5

    def test_observe_with_labels(self):
        h = Histogram("test_labeled", "test", label_names=("account",))
        h.observe(1.0, labels=("acct1",))
        h.observe(2.0, labels=("acct2",))
        snap = h.snapshot()
        assert len(snap["data"]) == 2


class TestGauge:
    def test_set_and_get(self):
        g = Gauge("test_gauge", "test")
        g.set(42.0)
        assert g.get() == 42.0

    def test_inc(self):
        g = Gauge("test_inc", "test")
        g.inc(5.0)
        g.inc(3.0)
        assert g.get() == 8.0

    def test_labels(self):
        g = Gauge("test_labeled_gauge", "test", label_names=("class",))
        g.set(10.0, labels=("options",))
        g.set(20.0, labels=("futures",))
        assert g.get(labels=("options",)) == 10.0
        assert g.get(labels=("futures",)) == 20.0


class TestCollectAllMetrics:
    def test_returns_all_metrics(self):
        metrics = collect_all_metrics()
        assert len(metrics) == 6
        names = {m["name"] for m in metrics}
        assert "phoenix_order_intent_to_ack_seconds" in names
        assert "phoenix_broker_sync_latency_seconds" in names
        assert "phoenix_quote_freshness_seconds" in names
