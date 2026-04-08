"""Tests for durable degraded recovery decision recording (M4)."""

from unittest.mock import patch

from app.core.degraded_scope_manager import (
    DegradedReason,
    DegradedScopeManager,
)


class TestDurableRecoveryRecording:
    def test_successful_recovery_emits_audit(self):
        mgr = DegradedScopeManager()
        mgr.enter_degraded(scope_key="scope1", reason=DegradedReason.LIFECYCLE_STUCK)

        with patch("app.core.degraded_scope_manager.DegradedScopeManager._record_recovery_decision") as mock_record:
            recovered = mgr.try_recover(
                scope_key="scope1",
                ownership_key_valid=True,
                broker_evidence_fresh=True,
                lifecycle_resolved=True,
                position_state_clean=True,
                actor="operator",
            )
            assert recovered is True
            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args[1]
            assert call_kwargs["recovered"] is True
            assert call_kwargs["actor"] == "operator"

    def test_failed_recovery_emits_audit(self):
        mgr = DegradedScopeManager()
        mgr.enter_degraded(scope_key="scope1", reason=DegradedReason.ATM_REMAP_FAILED)

        with patch("app.core.degraded_scope_manager.DegradedScopeManager._record_recovery_decision") as mock_record:
            recovered = mgr.try_recover(
                scope_key="scope1",
                ownership_key_valid=True,
                broker_evidence_fresh=False,
                lifecycle_resolved=True,
                position_state_clean=True,
                actor="system",
            )
            assert recovered is False
            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args[1]
            assert call_kwargs["recovered"] is False
            assert "broker_evidence" in call_kwargs["reason"]

    def test_operator_approval_required_blocks_auto_recovery(self):
        mgr = DegradedScopeManager()
        mgr.enter_degraded(scope_key="scope1", reason=DegradedReason.MANUAL_DEGRADATION)

        recovered = mgr.try_recover(
            scope_key="scope1",
            ownership_key_valid=True,
            broker_evidence_fresh=True,
            lifecycle_resolved=True,
            position_state_clean=True,
            require_operator_approval=True,
            operator_approved=False,
        )
        assert recovered is False

    def test_operator_approval_allows_recovery(self):
        mgr = DegradedScopeManager()
        mgr.enter_degraded(scope_key="scope1", reason=DegradedReason.MANUAL_DEGRADATION)

        recovered = mgr.try_recover(
            scope_key="scope1",
            ownership_key_valid=True,
            broker_evidence_fresh=True,
            lifecycle_resolved=True,
            position_state_clean=True,
            require_operator_approval=True,
            operator_approved=True,
        )
        assert recovered is True
