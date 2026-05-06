"""Sprint 6 OBS-6.1: Prometheus metrics tests."""

from __future__ import annotations

import pytest

from app.observability.prometheus_metrics import (
    counter_inc,
    gauge_set,
    histogram_observe,
    render_metrics,
    reset_metrics,
    record_order_submitted,
    record_order_latency,
    record_policy_decision,
    record_circuit_breaker_trip,
    record_tick_received,
    record_position_sync,
    record_realized_pnl,
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


class TestCounterInc:
    def test_basic_increment(self):
        counter_inc("test_counter", 1.0)
        output = render_metrics()
        assert "test_counter 1" in output

    def test_increment_with_labels(self):
        counter_inc("labeled_counter", labels={"env": "test"})
        output = render_metrics()
        assert 'labeled_counter{env="test"} 1' in output

    def test_accumulates(self):
        counter_inc("accum", 3.0)
        counter_inc("accum", 2.0)
        output = render_metrics()
        assert "accum 5" in output

    def test_separate_label_sets(self):
        counter_inc("multi", labels={"a": "1"})
        counter_inc("multi", labels={"a": "2"})
        output = render_metrics()
        assert 'multi{a="1"} 1' in output
        assert 'multi{a="2"} 1' in output


class TestGaugeSet:
    def test_basic_set(self):
        gauge_set("test_gauge", 42.5)
        output = render_metrics()
        assert "test_gauge 42.5" in output

    def test_overwrites_value(self):
        gauge_set("g", 10.0)
        gauge_set("g", 20.0)
        output = render_metrics()
        assert "g 20" in output
        assert "g 10" not in output


class TestHistogramObserve:
    def test_creates_count_and_sum(self):
        histogram_observe("latency", 0.5)
        histogram_observe("latency", 1.5)
        output = render_metrics()
        assert "latency_count 2" in output
        assert "latency_sum 2" in output


class TestRenderMetrics:
    def test_includes_uptime(self):
        output = render_metrics()
        assert "phoenix_uptime_seconds" in output

    def test_includes_type_annotations(self):
        counter_inc("typed_metric", help_text="A test metric")
        output = render_metrics()
        assert "# HELP typed_metric" in output
        assert "# TYPE typed_metric counter" in output

    def test_empty_store_renders_uptime(self):
        output = render_metrics()
        assert "phoenix_uptime_seconds" in output
        lines = [line for line in output.splitlines() if line and not line.startswith("#")]
        assert len(lines) >= 1


class TestKPIRecorders:
    def test_record_order_submitted(self):
        record_order_submitted("t1", "ba1", "strat1", "filled")
        output = render_metrics()
        assert "phoenix_orders_total" in output
        assert 'status="filled"' in output

    def test_record_order_latency(self):
        record_order_latency(0.123, "strat1")
        output = render_metrics()
        assert "phoenix_order_latency_seconds_count" in output

    def test_record_policy_decision(self):
        record_policy_decision("capital_guard", "block")
        output = render_metrics()
        assert "phoenix_policy_decisions_total" in output

    def test_record_circuit_breaker_trip(self):
        record_circuit_breaker_trip("loss_streak")
        output = render_metrics()
        assert "phoenix_circuit_breaker_trips_total" in output

    def test_record_tick_received(self):
        record_tick_received("NIFTY")
        output = render_metrics()
        assert "phoenix_ticks_received_total" in output

    def test_record_position_sync(self):
        record_position_sync("ba1", True)
        output = render_metrics()
        assert "phoenix_position_syncs_total" in output

    def test_record_realized_pnl(self):
        record_realized_pnl("t1", "ba1", -1500.0)
        output = render_metrics()
        assert "phoenix_realized_pnl" in output


class TestResetMetrics:
    def test_clears_all(self):
        counter_inc("will_be_cleared")
        reset_metrics()
        output = render_metrics()
        assert "will_be_cleared" not in output
