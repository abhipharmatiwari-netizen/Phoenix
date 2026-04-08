"""Tests for hub-authoritative LIVE enforcement of legacy exit blocking.

Covers:
- RiskManager._assert_direct_submit_allowed blocks in HUB_AUTHORITATIVE LIVE
- RiskManager._assert_direct_submit_allowed allows in LEGACY_AUTHORITATIVE
- RiskManager._assert_direct_submit_allowed allows in non-LIVE modes
- assert_legacy_exit_allowed blocks stream-side exits in HUB_AUTHORITATIVE
"""

import pytest
from unittest.mock import MagicMock

from app.core.p0_operational_guards import P0RuleViolation, assert_legacy_exit_allowed
from app.core.risk_manager import RiskManager


def _make_risk_manager(operating_mode: str = "") -> RiskManager:
    """Build a minimal RiskManager for guard testing."""
    rm = RiskManager(
        instrument_meta={},
        order_client=MagicMock(),
    )
    rm.set_operating_mode(operating_mode)
    return rm


class TestRiskManagerDirectSubmitGuard:
    def test_blocks_in_hub_authoritative_live(self):
        rm = _make_risk_manager("HUB_AUTHORITATIVE")
        with pytest.raises(P0RuleViolation, match="Rule 12"):
            rm._assert_direct_submit_allowed(
                trade_mode="LIVE",
                exit_source="square_off_all",
                label="NIFTY_CE_25000",
            )

    def test_allows_in_legacy_authoritative_live(self):
        rm = _make_risk_manager("LEGACY_AUTHORITATIVE")
        rm._assert_direct_submit_allowed(
            trade_mode="LIVE",
            exit_source="square_off_all",
            label="NIFTY_CE_25000",
        )

    def test_allows_in_paper_mode_even_hub(self):
        rm = _make_risk_manager("HUB_AUTHORITATIVE")
        rm._assert_direct_submit_allowed(
            trade_mode="PAPER",
            exit_source="square_off_all",
            label="NIFTY_CE_25000",
        )

    def test_allows_when_operating_mode_unset(self):
        rm = _make_risk_manager("")
        rm._assert_direct_submit_allowed(
            trade_mode="LIVE",
            exit_source="square_off_all",
            label="NIFTY_CE_25000",
        )

    def test_break_glass_allowed_in_hub_authoritative(self):
        """Break-glass exits are allowed even in HUB_AUTHORITATIVE."""
        assert_legacy_exit_allowed(
            operating_mode="HUB_AUTHORITATIVE",
            is_break_glass=True,
            exit_source="admin_breakglass",
            scope_key="acct:sym",
        )


class TestStreamSideLegacyExitGuard:
    """Verifies assert_legacy_exit_allowed blocks stream-side exits."""

    def test_profit_sweep_blocked_in_hub_live(self):
        with pytest.raises(P0RuleViolation, match="Rule 12"):
            assert_legacy_exit_allowed(
                operating_mode="HUB_AUTHORITATIVE",
                is_break_glass=False,
                exit_source="profit_sweep",
                scope_key="stream:profit_sweep",
            )

    def test_strict_intraday_blocked_in_hub_live(self):
        with pytest.raises(P0RuleViolation, match="Rule 12"):
            assert_legacy_exit_allowed(
                operating_mode="HUB_AUTHORITATIVE",
                is_break_glass=False,
                exit_source="strict_intraday_squareoff",
                scope_key="stream:strict_intraday",
            )

    def test_eod_square_off_blocked_in_hub_live(self):
        with pytest.raises(P0RuleViolation, match="Rule 12"):
            assert_legacy_exit_allowed(
                operating_mode="HUB_AUTHORITATIVE",
                is_break_glass=False,
                exit_source="eod_square_off",
                scope_key="stream:eod_square_off",
            )

    def test_profit_sweep_allowed_in_legacy(self):
        assert_legacy_exit_allowed(
            operating_mode="LEGACY_AUTHORITATIVE",
            is_break_glass=False,
            exit_source="profit_sweep",
            scope_key="stream:profit_sweep",
        )

    def test_eod_allowed_in_legacy(self):
        assert_legacy_exit_allowed(
            operating_mode="LEGACY_AUTHORITATIVE",
            is_break_glass=False,
            exit_source="eod_square_off",
            scope_key="stream:eod_square_off",
        )
