from __future__ import annotations

from app.core.signal_metrics import get_labeled_metrics


def test_labeled_metrics_registry_counter_and_gauge_roundtrip():
    metrics = get_labeled_metrics()
    metrics.reset_for_tests()

    labels = {"underlying": "NG_FUT", "tenant": "default", "regime": "TRENDING"}
    metrics.inc_counter("policy_apply_total", labels=labels)
    metrics.inc_counter("policy_apply_total", labels=labels, amount=2)
    metrics.set_gauge("dynamic_enabled_gauge", labels={"underlying": "NG_FUT", "tenant": "default"}, value=1)

    assert metrics.counter_value("policy_apply_total", labels=labels) == 3.0
    assert metrics.gauge_value(
        "dynamic_enabled_gauge",
        labels={"underlying": "NG_FUT", "tenant": "default"},
    ) == 1.0
