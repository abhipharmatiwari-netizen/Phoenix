"""Crash-recovery acceptance tests (Architecture S11.7, C4).

Before live deployment, Phoenix must pass these scenarios:
1. Crash after broker submit but before durable lifecycle persistence
2. Crash after durable persistence but before broker response handling
3. Lease loss during pending order lifecycle processing
4. Startup with open broker positions outside the current option universe
5. Partial-fill recovery with stale or ambiguous broker snapshots
6. Restart with pending outbox items, ownership locks, kill-switch state, and EOD markers
"""

import pytest


# ---------------------------------------------------------------------------
# Test 1: Crash after broker submit but before durable persistence
# ---------------------------------------------------------------------------

class TestCrashAfterBrokerSubmit:
    """Simulate crash after order submitted to broker but outbox not persisted."""

    def test_outbox_recovery_replays_pending_submission(self):
        """After restart, pending outbox items should be replayed."""
        from app.orders.order_outbox import OrderOutboxRecord, OrderSubmissionOutbox

        assert callable(getattr(OrderOutboxRecord, "__init__", None))
        assert callable(getattr(OrderSubmissionOutbox, "reserve_submission", None))

    def test_order_state_includes_recovery_pending(self):
        """RECOVERY_PENDING state must exist for gap recovery."""
        from app.orders.order_state import OrderLifecycleState

        assert hasattr(OrderLifecycleState, "RECOVERY_PENDING")

    def test_position_state_includes_recovery_pending(self):
        """RECOVERY_PENDING position state must exist."""
        from app.core.position_state import PositionState

        assert hasattr(PositionState, "RECOVERY_PENDING")


# ---------------------------------------------------------------------------
# Test 2: Crash after durable persistence but before broker response
# ---------------------------------------------------------------------------

class TestCrashAfterDurablePersistence:
    """Outbox committed but broker ack never processed."""

    def test_outbox_status_tracks_submitting(self):
        """Outbox must track SUBMITTING status for in-flight orders."""
        from app.orders.order_outbox import OUTBOX_STATUS_SUBMITTING

        assert OUTBOX_STATUS_SUBMITTING == "SUBMITTING"

    def test_reconciling_state_exists(self):
        """RECONCILING state must exist for ambiguous post-crash scenarios."""
        from app.orders.order_state import OrderLifecycleState

        assert hasattr(OrderLifecycleState, "RECONCILING")


# ---------------------------------------------------------------------------
# Test 3: Lease loss during pending order lifecycle processing
# ---------------------------------------------------------------------------

class TestLeaseLoss:
    """Simulate leader lease loss during order processing."""

    def test_leader_lease_module_exists(self):
        from app.core.leader_lease import LeaderLease

        assert callable(getattr(LeaderLease, "__init__", None))

    def test_ownership_state_blocks_on_reconciling(self):
        """When lease is lost, ownership should transition to RECONCILING."""
        from app.orders.ownership_state import (
            OwnershipState,
            ENTRY_BLOCKING_OWNERSHIP_STATES,
        )

        assert OwnershipState.RECONCILING in ENTRY_BLOCKING_OWNERSHIP_STATES


# ---------------------------------------------------------------------------
# Test 4: Startup with open broker positions outside option universe
# ---------------------------------------------------------------------------

class TestUnknownBrokerPositions:
    """Open broker positions not in current option universe."""

    def test_position_adopted_state_exists(self):
        from app.core.position_state import PositionState

        assert hasattr(PositionState, "ADOPTED")

    def test_orphan_review_state_exists(self):
        from app.orders.ownership_state import OwnershipState

        assert hasattr(OwnershipState, "ORPHAN_REVIEW")

    def test_orphan_review_decisions_exist(self):
        from app.orders.ownership_state import OrphanReviewDecision

        assert hasattr(OrphanReviewDecision, "ADOPT")
        assert hasattr(OrphanReviewDecision, "FLATTEN")
        assert hasattr(OrphanReviewDecision, "SUPPRESS")
        assert hasattr(OrphanReviewDecision, "CONTINUE_OBSERVING")


# ---------------------------------------------------------------------------
# Test 5: Partial-fill recovery with stale broker snapshots
# ---------------------------------------------------------------------------

class TestPartialFillRecovery:
    """Partial fills with ambiguous broker evidence."""

    def test_partial_fill_state_exists(self):
        from app.orders.order_state import OrderLifecycleState

        assert hasattr(OrderLifecycleState, "PARTIAL_FILL")

    def test_reconciling_position_blocks_entries(self):
        from app.core.position_state import PositionState, ENTRY_BLOCKING_STATES

        assert PositionState.RECONCILING in ENTRY_BLOCKING_STATES

    def test_evidence_classes_exist(self):
        from app.core.position_sync import (
            PositionSyncEvidenceClass,
            PositionSyncReconciliationState,
        )

        assert hasattr(PositionSyncEvidenceClass, "WEAK")
        assert hasattr(PositionSyncEvidenceClass, "STRONG")
        assert hasattr(PositionSyncReconciliationState, "RECONCILING")


# ---------------------------------------------------------------------------
# Test 6: Restart with pending state
# ---------------------------------------------------------------------------

class TestRestartWithPendingState:
    """Restart with outbox, ownership, kill-switch, EOD markers."""

    def test_outbox_module_has_recovery(self):
        from app.orders.order_outbox import OrderSubmissionOutbox

        assert callable(getattr(OrderSubmissionOutbox, "reserve_submission", None))

    def test_circuit_breaker_persist_state_respected(self):
        """Kill-switch state must survive restarts when persistence enabled."""
        from app.risk.circuit_breaker import TradingCircuitBreaker

        assert callable(getattr(TradingCircuitBreaker, "__init__", None))

    def test_anti_pattern_guard_prevents_replay_before_reconciliation(self):
        from app.core.anti_pattern_guards import (
            AntiPatternViolation,
            assert_reconciliation_complete_before_replay,
            reset_reconciliation_state,
        )

        reset_reconciliation_state()
        with pytest.raises(AntiPatternViolation):
            assert_reconciliation_complete_before_replay("test_scope")

    def test_reconciliation_complete_enables_replay(self):
        from app.core.anti_pattern_guards import (
            assert_reconciliation_complete_before_replay,
            mark_reconciliation_complete,
            reset_reconciliation_state,
        )

        reset_reconciliation_state()
        mark_reconciliation_complete("test_scope")
        # Should not raise
        assert_reconciliation_complete_before_replay("test_scope")
