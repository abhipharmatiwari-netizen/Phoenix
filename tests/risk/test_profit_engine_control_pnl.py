"""Tests for ProfitEngine using control_pnl for order entry check (#86, #87).

Verifies that:
- ProfitEngine.check_order_allowed() prefers get_control_total_pnl() over
  get_current_total_pnl() to prevent the short-option SELL premium from
  prematurely arming the profit lock (issue #86).
- The reason string is factually correct when the lock is active from a
  historical peak but current total_pnl < target (issue #87).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.risk.profit_engine import ProfitDecision, ProfitEngine
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId


def _settings(**overrides):
    defaults = dict(
        enable_profit_checks=True,
        profit_enable_daily_target=True,
        profit_daily_target=5000.0,
        profit_block_new_orders_on_target=True,
        profit_lock_enabled=True,
        profit_lock_giveback_pct=0.1,
        profit_lock_require_signal=False,
        profit_lock_exit_cooldown_seconds=0.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_pnl_engine(*, cash_total: float, control_total):
    """Return a mock PnLEngine with preset cash and control PnL values."""
    eng = MagicMock()
    eng.get_current_total_pnl.return_value = cash_total
    eng.get_control_total_pnl.return_value = control_total
    return eng


# ---------------------------------------------------------------------------
# Issue #86: profit lock must NOT fire on SELL entry fill alone
# ---------------------------------------------------------------------------

def test_profit_check_uses_control_pnl_not_cash_pnl():
    """When control_pnl is available, check_order_allowed uses it instead of cash ledger.

    Scenario: SELL 65 NIFTY CE @ 202.75 → cash PnL = +13,178 (premium received).
    control_pnl = 0 (position not yet closed).  Daily target = 5000.
    Expected: entry ALLOWED because control_pnl (0) < target (5000).
    """
    pnl_engine = _mock_pnl_engine(
        cash_total=13_178.75,   # inflated by SELL fill (the bug scenario)
        control_total=0.0,      # no economic close yet
    )
    engine = ProfitEngine(pnl_engine=pnl_engine)

    with patch("app.risk.profit_engine.get_settings", return_value=_settings()):
        with patch("app.risk.profit_engine.get_runtime_settings", return_value=_settings()):
            result = engine.check_order_allowed(
                tenant_id=TenantId("t1"),
                broker_account_id=BrokerAccountId("A1"),
                strategy_id=StrategyId("ema20_strategy"),
            )

    assert result.allowed is True, (
        f"Entry must be ALLOWED when control_pnl=0 < target=5000, got: {result.reason}"
    )


def test_profit_check_allows_entry_when_control_pnl_below_target():
    """control_pnl=962 < target=5000 → entry allowed."""
    pnl_engine = _mock_pnl_engine(cash_total=962.0, control_total=962.0)
    engine = ProfitEngine(pnl_engine=pnl_engine)

    with patch("app.risk.profit_engine.get_settings", return_value=_settings()):
        with patch("app.risk.profit_engine.get_runtime_settings", return_value=_settings()):
            result = engine.check_order_allowed(
                tenant_id=TenantId("t1"),
                broker_account_id=BrokerAccountId("A1"),
                strategy_id=StrategyId("ema20_strategy"),
            )

    assert result.allowed is True, f"Unexpected block: {result.reason}"


def test_profit_check_blocks_when_control_pnl_exceeds_target():
    """When real economic profit (control_pnl) exceeds target, new entries are blocked."""
    pnl_engine = _mock_pnl_engine(cash_total=6000.0, control_total=6000.0)
    engine = ProfitEngine(pnl_engine=pnl_engine)

    with patch("app.risk.profit_engine.get_settings", return_value=_settings()):
        with patch("app.risk.profit_engine.get_runtime_settings", return_value=_settings()):
            result = engine.check_order_allowed(
                tenant_id=TenantId("t1"),
                broker_account_id=BrokerAccountId("A1"),
                strategy_id=StrategyId("ema20_strategy"),
            )

    assert result.allowed is False, f"Expected block when control_pnl=6000 >= target=5000"


def test_profit_check_falls_back_to_cash_when_no_control_data():
    """When get_control_total_pnl returns None (no short-option data), fall back to cash ledger."""
    pnl_engine = _mock_pnl_engine(cash_total=700.0, control_total=None)
    engine = ProfitEngine(pnl_engine=pnl_engine)

    with patch("app.risk.profit_engine.get_settings", return_value=_settings()):
        with patch("app.risk.profit_engine.get_runtime_settings", return_value=_settings()):
            result = engine.check_order_allowed(
                tenant_id=TenantId("t1"),
                broker_account_id=BrokerAccountId("A1"),
                strategy_id=StrategyId("ema20_strategy"),
            )

    assert result.allowed is True, (
        f"Cash fallback: 700 < 5000 should be allowed. Got: {result.reason}"
    )
    # Verify cash path was consulted when control returned None
    pnl_engine.get_current_total_pnl.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #87: reason string must be factually accurate
# ---------------------------------------------------------------------------

def test_reason_string_correct_when_lock_active_from_historical_peak(monkeypatch):
    """When lock is active from a historical peak but current < target, reason must NOT
    claim 'total_pnl >= target'."""
    from app.pnl.profit_lock import ProfitLockManager, ProfitLockDecision

    # Simulate a lock that was triggered by a historical peak (e.g., cash PnL spiked
    # to 13,178 from SELL fill) and is now holding at current=1280 < target=5000.
    fake_decision = ProfitLockDecision(
        lock_active=True,
        lock_floor=11860.875,
        peak_total_pnl=13178.75,
        exit_required=False,
        exit_reason=None,
        signal_valid=None,
        target=5000.0,
    )
    mock_lock = MagicMock(spec=ProfitLockManager)
    mock_lock.evaluate.return_value = fake_decision

    pnl_engine = _mock_pnl_engine(cash_total=1280.5, control_total=962.0)
    engine = ProfitEngine(pnl_engine=pnl_engine, profit_lock_manager=mock_lock)

    with patch("app.risk.profit_engine.get_settings", return_value=_settings()):
        with patch("app.risk.profit_engine.get_runtime_settings", return_value=_settings()):
            result = engine.check_order_allowed(
                tenant_id=TenantId("t1"),
                broker_account_id=BrokerAccountId("A1"),
                strategy_id=StrategyId("ema20_strategy"),
            )

    assert result.allowed is False
    # The reason must NOT claim 962 >= 5000 (factually false)
    assert ">= target" not in result.reason, (
        f"Reason string must not use '>= target' when current < target: {result.reason}"
    )
    assert "profit_lock_active" in result.reason, (
        f"Expected 'profit_lock_active' in reason when lock is from historical peak: {result.reason}"
    )
    assert "peak_pnl" in result.reason, (
        f"Expected 'peak_pnl' in reason: {result.reason}"
    )
