"""Tests for extended anti-pattern guards (H3, M5)."""

import pytest

from app.core.anti_pattern_guards import (
    AntiPatternViolation,
    assert_not_restart_helper_authority,
    assert_runner_feeds_through_lifecycle,
)


class TestRestartHelperAuthorityGuard:
    """H3: Prevent risk_positions.json auto-adoption without broker reconciliation."""

    def test_json_without_reconciliation_raises(self):
        with pytest.raises(AntiPatternViolation, match="risk_positions.json"):
            assert_not_restart_helper_authority(
                source="risk_positions.json",
                has_broker_reconciliation=False,
            )

    def test_json_with_reconciliation_ok(self):
        assert_not_restart_helper_authority(
            source="risk_positions.json",
            has_broker_reconciliation=True,
        )

    def test_restart_helper_without_reconciliation_raises(self):
        with pytest.raises(AntiPatternViolation):
            assert_not_restart_helper_authority(
                source="restart_helper",
                has_broker_reconciliation=False,
            )

    def test_broker_source_without_reconciliation_ok(self):
        # Non-restart-helper sources are fine
        assert_not_restart_helper_authority(
            source="broker_snapshot",
            has_broker_reconciliation=False,
        )

    def test_stale_json_position_rejected_without_evidence(self):
        """Stale JSON position must be rejected when broker evidence contradicts."""
        with pytest.raises(AntiPatternViolation):
            assert_not_restart_helper_authority(
                source="risk_positions",
                has_broker_reconciliation=False,
            )


class TestRunnerFeedsThroughLifecycle:
    """M5: AccountRunner results must flow through OrderLifecycleService."""

    def test_direct_runner_mutation_blocked(self):
        with pytest.raises(AntiPatternViolation, match="OrderLifecycleService"):
            assert_runner_feeds_through_lifecycle(
                caller="account_runner_direct",
                action="update_position",
            )

    def test_runner_bypass_blocked(self):
        with pytest.raises(AntiPatternViolation):
            assert_runner_feeds_through_lifecycle(
                caller="runner_bypass",
                action="commit_fill",
            )

    def test_lifecycle_service_caller_ok(self):
        # Legitimate callers should not be blocked
        assert_runner_feeds_through_lifecycle(
            caller="order_lifecycle_service",
            action="process_fill",
        )
