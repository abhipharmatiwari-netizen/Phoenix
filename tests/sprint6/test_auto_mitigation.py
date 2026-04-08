"""Sprint 6 OPS-6.5: Auto-mitigation tests."""

from __future__ import annotations

import pytest

from app.observability.auto_mitigation import (
    FaultCategory,
    FaultTracker,
    MitigationAction,
    MitigationRule,
    build_default_mitigation_rules,
    get_fault_tracker,
    reset_fault_tracker,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_fault_tracker()
    yield
    reset_fault_tracker()


class TestFaultTracker:
    def test_record_fault_no_rules(self):
        tracker = FaultTracker()
        events = tracker.record_fault(FaultCategory.ORDER_REJECTION, "strat1")
        assert events == []

    def test_triggers_mitigation_at_threshold(self):
        rule = MitigationRule(
            name="test_rule",
            fault_category=FaultCategory.ORDER_REJECTION,
            action=MitigationAction.LOG_ONLY,
            threshold=3,
            window_seconds=60.0,
            scope_key="strategy_id",
        )
        tracker = FaultTracker([rule])

        # Below threshold — no mitigation
        tracker.record_fault(FaultCategory.ORDER_REJECTION, "strat1")
        events = tracker.record_fault(FaultCategory.ORDER_REJECTION, "strat1")
        assert events == []

        # At threshold — fires
        events = tracker.record_fault(FaultCategory.ORDER_REJECTION, "strat1")
        assert len(events) == 1
        assert events[0].rule_name == "test_rule"
        assert events[0].action == MitigationAction.LOG_ONLY
        assert events[0].fault_count == 3

    def test_cooldown_prevents_repeated_firing(self):
        rule = MitigationRule(
            name="cooldown_test",
            fault_category=FaultCategory.BROKER_TIMEOUT,
            action=MitigationAction.LOG_ONLY,
            threshold=1,
            window_seconds=60.0,
            cooldown_seconds=9999.0,
        )
        tracker = FaultTracker([rule])

        events1 = tracker.record_fault(FaultCategory.BROKER_TIMEOUT, "ba1")
        assert len(events1) == 1

        # Second fault within cooldown — should NOT fire again
        events2 = tracker.record_fault(FaultCategory.BROKER_TIMEOUT, "ba1")
        assert len(events2) == 0

    def test_different_categories_independent(self):
        rule = MitigationRule(
            name="cat_test",
            fault_category=FaultCategory.ORDER_REJECTION,
            action=MitigationAction.LOG_ONLY,
            threshold=1,
            window_seconds=60.0,
        )
        tracker = FaultTracker([rule])

        # Different category — should NOT trigger
        events = tracker.record_fault(FaultCategory.BROKER_TIMEOUT, "x")
        assert events == []

        # Correct category — should trigger
        events = tracker.record_fault(FaultCategory.ORDER_REJECTION, "x")
        assert len(events) == 1

    def test_action_handler_called(self):
        captured = []
        rule = MitigationRule(
            name="handler_test",
            fault_category=FaultCategory.UNHANDLED_EXCEPTION,
            action=MitigationAction.DISABLE_STRATEGY,
            threshold=1,
            window_seconds=60.0,
        )
        tracker = FaultTracker([rule])
        tracker.set_action_handler(
            MitigationAction.DISABLE_STRATEGY,
            lambda evt: captured.append(evt),
        )

        tracker.record_fault(FaultCategory.UNHANDLED_EXCEPTION, "strat1")
        assert len(captured) == 1
        assert captured[0].scope_value == "strat1"

    def test_disabled_tracker_records_but_no_mitigation(self):
        rule = MitigationRule(
            name="disabled_test",
            fault_category=FaultCategory.ORDER_REJECTION,
            action=MitigationAction.LOG_ONLY,
            threshold=1,
            window_seconds=60.0,
        )
        tracker = FaultTracker([rule])
        tracker.enabled = False

        events = tracker.record_fault(FaultCategory.ORDER_REJECTION, "x")
        assert events == []
        # But fault is still recorded
        counts = tracker.get_fault_counts()
        assert counts["order_rejection"]["x"] == 1

    def test_get_events(self):
        rule = MitigationRule(
            name="events_test",
            fault_category=FaultCategory.ORDER_REJECTION,
            action=MitigationAction.LOG_ONLY,
            threshold=1,
            window_seconds=60.0,
            cooldown_seconds=0,
        )
        tracker = FaultTracker([rule])

        tracker.record_fault(FaultCategory.ORDER_REJECTION, "a")
        tracker.record_fault(FaultCategory.ORDER_REJECTION, "b")
        events = tracker.get_events()
        assert len(events) == 2

    def test_reset_clears_state(self):
        rule = MitigationRule(
            name="reset_test",
            fault_category=FaultCategory.ORDER_REJECTION,
            action=MitigationAction.LOG_ONLY,
            threshold=1,
            window_seconds=60.0,
        )
        tracker = FaultTracker([rule])
        tracker.record_fault(FaultCategory.ORDER_REJECTION, "x")
        tracker.reset()
        assert tracker.get_fault_counts() == {}
        assert tracker.get_events() == []

    def test_summary(self):
        tracker = FaultTracker(build_default_mitigation_rules())
        summary = tracker.summary()
        assert summary["enabled"] is True
        assert summary["rule_count"] >= 4
        assert "fault_counts" in summary


class TestDefaultRules:
    def test_default_rules_built(self):
        rules = build_default_mitigation_rules()
        assert len(rules) >= 4
        names = {r.name for r in rules}
        assert "strategy_rejection_storm" in names
        assert "broker_timeout_breaker" in names

    def test_singleton_tracker(self):
        t1 = get_fault_tracker()
        t2 = get_fault_tracker()
        assert t1 is t2
