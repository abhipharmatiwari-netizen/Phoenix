"""Sprint 6 OBS-6.3: Alert rules and threshold evaluation tests."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.observability.alert_rules import (
    AlertEvaluator,
    AlertRule,
    AlertSeverity,
    AlertState,
    build_default_alert_rules,
    get_alert_evaluator,
    reset_alert_evaluator,
)
from app.observability.prometheus_metrics import (
    counter_inc,
    gauge_set,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_metrics()
    reset_alert_evaluator()
    yield
    reset_metrics()
    reset_alert_evaluator()


class TestAlertEvaluator:
    def test_empty_evaluator(self):
        ev = AlertEvaluator()
        results = ev.evaluate_all()
        assert results == []

    def test_ok_rule(self):
        rule = AlertRule(
            name="test_ok",
            description="Always OK",
            severity=AlertSeverity.INFO,
            evaluate_fn=lambda: (False, 0, ""),
        )
        ev = AlertEvaluator([rule])
        results = ev.evaluate_all()
        assert len(results) == 1
        assert results[0].state == AlertState.OK

    def test_firing_rule(self):
        rule = AlertRule(
            name="test_fire",
            description="Always fires",
            severity=AlertSeverity.CRITICAL,
            evaluate_fn=lambda: (True, 99, "bad stuff"),
        )
        ev = AlertEvaluator([rule])
        results = ev.evaluate_all()
        assert len(results) == 1
        assert results[0].state == AlertState.FIRING
        assert results[0].value == 99
        assert results[0].message == "bad stuff"

    def test_firing_then_resolved(self):
        state = {"firing": True}
        rule = AlertRule(
            name="toggle",
            description="Toggle",
            severity=AlertSeverity.WARNING,
            evaluate_fn=lambda: (state["firing"], 1, "problem"),
        )
        ev = AlertEvaluator([rule])

        ev.evaluate_all()
        assert ev.get_firing_alerts()[0].state == AlertState.FIRING

        state["firing"] = False
        ev.evaluate_all()
        statuses = ev.get_all_statuses()
        assert statuses[0].state == AlertState.RESOLVED

    def test_debounce_for_seconds(self):
        rule = AlertRule(
            name="debounced",
            description="Requires 10s",
            severity=AlertSeverity.WARNING,
            evaluate_fn=lambda: (True, 1, "pending"),
            for_seconds=10.0,
        )
        ev = AlertEvaluator([rule])
        results = ev.evaluate_all()
        # Should NOT be firing yet (hasn't been 10s)
        assert results[0].state == AlertState.OK

    def test_evaluation_error_handled(self):
        def bad_fn():
            raise RuntimeError("boom")

        rule = AlertRule(
            name="broken",
            description="Broken eval",
            severity=AlertSeverity.INFO,
            evaluate_fn=bad_fn,
        )
        ev = AlertEvaluator([rule])
        results = ev.evaluate_all()
        assert results[0].state == AlertState.OK

    def test_summary(self):
        rule = AlertRule(
            name="test",
            description="Test",
            severity=AlertSeverity.INFO,
            evaluate_fn=lambda: (True, 1, "firing"),
        )
        ev = AlertEvaluator([rule])
        summary = ev.summary()
        assert summary["total_rules"] == 1
        assert summary["firing_count"] == 1
        assert len(summary["alerts"]) == 1

    def test_labels_propagated(self):
        rule = AlertRule(
            name="labeled",
            description="Has labels",
            severity=AlertSeverity.INFO,
            evaluate_fn=lambda: (True, 1, "x"),
            labels={"component": "test"},
        )
        ev = AlertEvaluator([rule])
        results = ev.evaluate_all()
        assert results[0].labels["component"] == "test"


class TestBuiltInRules:
    def test_default_rules_created(self):
        rules = build_default_alert_rules()
        assert len(rules) >= 5
        names = {r.name for r in rules}
        assert "circuit_breaker_tripped" in names
        assert "high_order_rejection_rate" in names
        assert "position_sync_failures" in names
        assert "tick_data_gaps" in names
        assert "pnl_drawdown_critical" in names

    def test_circuit_breaker_rule_fires(self):
        counter_inc("phoenix_circuit_breaker_trips_total", labels={"type": "loss_streak"})
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        cb_alert = next(r for r in results if r.rule_name == "circuit_breaker_tripped")
        assert cb_alert.state == AlertState.FIRING

    def test_pnl_drawdown_fires(self):
        gauge_set("phoenix_realized_pnl", -75000.0, labels={"tenant_id": "t1", "broker_account_id": "ba1"})
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        pnl_alert = next(r for r in results if r.rule_name == "pnl_drawdown_critical")
        assert pnl_alert.state == AlertState.FIRING

    def test_ownership_conflicts_alert_fires(self):
        counter_inc("phoenix_ownership_conflicts_total", 4)
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        alert = next(r for r in results if r.rule_name == "ownership_conflicts")
        assert alert.state == AlertState.FIRING

    def test_deadletter_growth_alert_fires(self):
        gauge_set("phoenix_deadletter_count", 2)
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        alert = next(r for r in results if r.rule_name == "deadletter_growth")
        assert alert.state == AlertState.FIRING

    def test_leader_lease_failure_alert_fires(self):
        counter_inc("phoenix_lease_renewal_failures_total", 1)
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        alert = next(r for r in results if r.rule_name == "leader_lease_failure")
        assert alert.state == AlertState.FIRING

    def test_position_authority_readiness_alert_fires(self, monkeypatch):
        monkeypatch.setenv("TRADE_MODE", "LIVE")
        monkeypatch.setattr(
            "app.hub.runtime.get_hub_runtime",
            lambda: SimpleNamespace(
                order_lifecycle=SimpleNamespace(
                    count_positions_by_state=lambda: {"RECONCILING": 2}
                )
            ),
        )
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        alert = next(
            r for r in results if r.rule_name == "readiness_position_authority_degraded"
        )
        assert alert.state == AlertState.FIRING
        assert alert.value == 2

    def test_oi_ml_shadow_ingestion_alert_fires(self, monkeypatch):
        monkeypatch.setattr(
            "app.strategies.oi_ml.shadow_health.collect_shadow_ingestion_status",
            lambda: {
                "enabled": True,
                "status": "degraded",
                "reason": "option_chain_rows_missing",
                "option_chain": {"today_row_count": 0},
            },
        )
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        alert = next(r for r in results if r.rule_name == "oi_ml_shadow_ingestion_degraded")
        assert alert.state == AlertState.FIRING
        assert alert.value == 0
        assert "option_chain_rows_missing" in alert.message

    def test_stuck_orders_alert_fires(self):
        gauge_set("phoenix_stuck_orders_count", 1)
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        alert = next(r for r in results if r.rule_name == "stuck_orders")
        assert alert.state == AlertState.FIRING

    def test_no_alerts_fire_on_clean_state(self):
        ev = get_alert_evaluator()
        results = ev.evaluate_all()
        firing = [r for r in results if r.state == AlertState.FIRING]
        assert len(firing) == 0
