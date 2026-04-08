"""Tests for P0 operational rule guards (M7)."""

import pytest

from app.core.p0_operational_guards import (
    P0RuleViolation,
    assert_caller_is_authoritative,
    assert_dashboard_not_authoritative,
    assert_legacy_exit_allowed,
)


class TestRule9DashboardNonAuthoritative:
    def test_dashboard_caller_blocked(self):
        with pytest.raises(P0RuleViolation, match="Rule 9"):
            assert_dashboard_not_authoritative(caller="dashboard_bus", action="update_position")

    def test_websocket_caller_blocked(self):
        with pytest.raises(P0RuleViolation):
            assert_dashboard_not_authoritative(caller="websocket_handler", action="mutate")

    def test_legitimate_caller_allowed(self):
        assert_dashboard_not_authoritative(caller="order_lifecycle_service", action="fill")


class TestRule12LegacyExitBlock:
    def test_legacy_exit_blocked_in_hub_mode(self):
        with pytest.raises(P0RuleViolation, match="Rule 12"):
            assert_legacy_exit_allowed(
                operating_mode="HUB_AUTHORITATIVE",
                is_break_glass=False,
                exit_source="legacy_stream",
                scope_key="acct:sym",
            )

    def test_break_glass_allowed_in_hub_mode(self):
        assert_legacy_exit_allowed(
            operating_mode="HUB_AUTHORITATIVE",
            is_break_glass=True,
            exit_source="admin_breakglass",
            scope_key="acct:sym",
        )

    def test_legacy_exit_allowed_in_legacy_mode(self):
        assert_legacy_exit_allowed(
            operating_mode="LEGACY_AUTHORITATIVE",
            is_break_glass=False,
            exit_source="legacy_stream",
            scope_key="acct:sym",
        )


class TestRule21AuthoritativeMutators:
    def test_strategy_blocked(self):
        with pytest.raises(P0RuleViolation, match="Rule 21"):
            assert_caller_is_authoritative(
                caller="strategy_evaluator",
                action="commit_position",
                scope_key="acct:sym",
            )

    def test_indicator_blocked(self):
        with pytest.raises(P0RuleViolation, match="Rule 21"):
            assert_caller_is_authoritative(
                caller="indicator_engine",
                action="update_position",
                scope_key="acct:sym",
            )

    def test_lifecycle_service_allowed(self):
        assert_caller_is_authoritative(
            caller="order_lifecycle_service",
            action="fill",
            scope_key="acct:sym",
        )

    def test_state_store_allowed(self):
        assert_caller_is_authoritative(
            caller="state_store",
            action="persist",
            scope_key="acct:sym",
        )

    def test_admin_allowed(self):
        assert_caller_is_authoritative(
            caller="admin",
            action="override",
            scope_key="acct:sym",
        )
