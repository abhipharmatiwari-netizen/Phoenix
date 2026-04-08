"""Tests for alert rules (H5)."""

from app.core.alert_rules import (
    AlertEvaluator,
    AlertSeverity,
    DeadletterGrowthRule,
    LeaseRenewalFailureRule,
    OwnershipConflictSpikeRule,
    StuckOrdersRule,
    build_default_alert_evaluator,
)


class TestOwnershipConflictSpikeRule:
    def test_no_alert_below_threshold(self):
        rule = OwnershipConflictSpikeRule(
            pending_count_fn=lambda: {"scope1": 2},
            threshold=10,
        )
        assert rule.evaluate() is None

    def test_alert_above_threshold(self):
        rule = OwnershipConflictSpikeRule(
            pending_count_fn=lambda: {"s1": 5, "s2": 6},
            threshold=10,
        )
        event = rule.evaluate()
        assert event is not None
        assert event.severity == AlertSeverity.WARNING
        assert "11" in event.message


class TestDeadletterGrowthRule:
    def test_no_alert_when_stable(self):
        rule = DeadletterGrowthRule(
            deadletter_count_fn=lambda: 3,
            threshold=5,
        )
        assert rule.evaluate() is None

    def test_alert_on_growth_above_threshold(self):
        count = [0]
        def fn():
            count[0] += 6
            return count[0]
        rule = DeadletterGrowthRule(deadletter_count_fn=fn, threshold=5)
        event = rule.evaluate()
        assert event is not None
        assert event.severity == AlertSeverity.CRITICAL


class TestLeaseRenewalFailureRule:
    def test_no_alert_when_healthy(self):
        rule = LeaseRenewalFailureRule(lease_healthy_fn=lambda: True)
        assert rule.evaluate() is None

    def test_alert_after_consecutive_failures(self):
        rule = LeaseRenewalFailureRule(lease_healthy_fn=lambda: False)
        rule.evaluate()  # 1st failure
        event = rule.evaluate()  # 2nd failure
        assert event is not None
        assert event.severity == AlertSeverity.CRITICAL

    def test_reset_on_recovery(self):
        healthy = [False]
        rule = LeaseRenewalFailureRule(lease_healthy_fn=lambda: healthy[0])
        rule.evaluate()
        healthy[0] = True
        rule.evaluate()
        healthy[0] = False
        # Should need 2 more failures
        assert rule.evaluate() is None


class TestStuckOrdersRule:
    def test_no_alert_when_no_stuck(self):
        rule = StuckOrdersRule(stuck_orders_fn=lambda: [])
        assert rule.evaluate() is None

    def test_alert_when_stuck(self):
        rule = StuckOrdersRule(
            stuck_orders_fn=lambda: [{"order_id": "1", "age_seconds": 200}],
            threshold_seconds=120.0,
        )
        event = rule.evaluate()
        assert event is not None
        assert "stuck" in event.message.lower()


class TestAlertEvaluator:
    def test_evaluate_all_fires_matching_rules(self):
        evaluator = build_default_alert_evaluator(
            pending_count_fn=lambda: {"s1": 100},
            deadletter_count_fn=lambda: 0,
            lease_healthy_fn=lambda: True,
            stuck_orders_fn=lambda: [],
        )
        events = evaluator.evaluate_all()
        assert len(events) >= 1
        assert events[0].rule_name == "ownership_conflict_spike"

    def test_rule_count(self):
        evaluator = build_default_alert_evaluator()
        assert evaluator.rule_count() == 4
