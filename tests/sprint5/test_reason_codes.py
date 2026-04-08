"""Sprint 5 OBS-5.6: Reason codes and policy audit trail tests."""

from __future__ import annotations

import pytest

from app.orders.reason_codes import (
    PolicyAction,
    PolicyAuditTrail,
    PolicyDecision,
    PolicySource,
    RC_CAPITAL_INSUFFICIENT,
    RC_EXIT_ORDER_BYPASS,
    RC_EXPOSURE_MAX_POSITIONS,
    RC_KILL_SWITCH_ACTIVE,
)


class TestPolicyDecision:
    def test_to_dict(self):
        d = PolicyDecision(
            source=PolicySource.CAPITAL_GUARD,
            action=PolicyAction.BLOCK,
            reason_code=RC_CAPITAL_INSUFFICIENT,
            message="Not enough capital",
            details={"available": 50000, "required": 100000},
        )
        result = d.to_dict()
        assert result["source"] == "capital_guard"
        assert result["action"] == "block"
        assert result["reason_code"] == "CAPITAL_INSUFFICIENT"
        assert result["details"]["required"] == 100000


class TestPolicyAuditTrail:
    def test_empty_trail_allows(self):
        trail = PolicyAuditTrail()
        assert trail.final_action == PolicyAction.ALLOW
        assert not trail.blocked

    def test_single_block_blocks(self):
        trail = PolicyAuditTrail(hub_order_id="test-1")
        trail.record(PolicyDecision(
            source=PolicySource.KILL_SWITCH,
            action=PolicyAction.BLOCK,
            reason_code=RC_KILL_SWITCH_ACTIVE,
            message="Kill switch active",
        ))
        assert trail.blocked
        assert trail.final_action == PolicyAction.BLOCK
        assert len(trail.block_reasons) == 1

    def test_resize_is_not_block(self):
        trail = PolicyAuditTrail()
        trail.record(PolicyDecision(
            source=PolicySource.CAPITAL_RESIZE,
            action=PolicyAction.RESIZE,
            reason_code="CAPITAL_RESIZE_APPLIED",
            message="Resized to 2 lots",
        ))
        assert not trail.blocked
        assert trail.final_action == PolicyAction.RESIZE

    def test_block_overrides_resize(self):
        trail = PolicyAuditTrail()
        trail.record(PolicyDecision(
            source=PolicySource.CAPITAL_RESIZE,
            action=PolicyAction.RESIZE,
            reason_code="CAPITAL_RESIZE_APPLIED",
            message="Resized",
        ))
        trail.record(PolicyDecision(
            source=PolicySource.EXPOSURE_LIMIT,
            action=PolicyAction.BLOCK,
            reason_code=RC_EXPOSURE_MAX_POSITIONS,
            message="Max positions reached",
        ))
        assert trail.blocked

    def test_summary_includes_all_decisions(self):
        trail = PolicyAuditTrail(hub_order_id="order-123")
        trail.record(PolicyDecision(
            source=PolicySource.RISK_GUARD,
            action=PolicyAction.ALLOW,
            reason_code="RISK_OK",
            message="Risk check passed",
        ))
        trail.record(PolicyDecision(
            source=PolicySource.PROFIT_GUARD,
            action=PolicyAction.SKIP,
            reason_code=RC_EXIT_ORDER_BYPASS,
            message="Exit order — skipped",
        ))
        summary = trail.to_summary()
        assert summary["hub_order_id"] == "order-123"
        assert summary["decision_count"] == 2
        assert summary["final_action"] == "allow"
