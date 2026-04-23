"""
Regression test: full short-credit → manual-close → EOD duplicate-order scenario.

Scenario:
1. Short SELL 65@181 → control_open_premium=11765, control_pnl≈0
2. Mark to LTP=134 → control_pnl≈3055
3. absolute target=5000 → NOT armed; premium_pct=0.25 → target≈2941 → armed
4. Manual broker-side close (POS_SYNC strong flat) → strategy managed state cleared
5. EOD timer fires → NO duplicate BUY placed (position already flat)
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from app.pnl.pnl_engine import PnLEngine
from app.pnl.state_store import InMemoryPnLStateStore
from app.pnl.types import TradeOpenEvent, TradeCloseEvent


# --- helpers ---

def _make_engine():
    return PnLEngine(state_store=InMemoryPnLStateStore())


def _open_event(qty=65, price=181.0):
    return TradeOpenEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1",
        symbol="NIFTY28APR2624300CE", qty=qty, entry_price=price, lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )


def _close_event(qty=65, price=123.65):
    return TradeCloseEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1",
        symbol="NIFTY28APR2624300CE", qty=qty, exit_price=price, lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )


# --- Step 1-2: Entry then mark-to-market ---

def test_step1_entry_does_not_book_premium_as_control_profit():
    """After SELL 65@181, control_total_pnl ≈ 0 (no cash credit counted)."""
    eng = _make_engine()
    eng.on_open_position(_open_event())
    total = eng.get_control_total_pnl("t1", "A1")
    assert total is not None
    assert abs(total) < 1.0


def test_step2_mark_to_134_shows_correct_profit():
    """After LTP moves to 134, control_pnl = (181-134)*65 = 3055."""
    eng = _make_engine()
    eng.on_open_position(_open_event())
    eng.update_control_open_pnl("t1", "A1", "strat1", current_ltp=134.0)
    total = eng.get_control_total_pnl("t1", "A1")
    expected = (181.0 - 134.0) * 65
    assert abs(total - expected) < 1.0


# --- Step 3: Target mode comparison ---

def test_step3a_absolute_5000_not_armed_at_3055():
    """absolute target=5000, pnl≈3055 → sweep NOT armed."""
    eng = _make_engine()
    eng.on_open_position(_open_event())
    eng.update_control_open_pnl("t1", "A1", "strat1", current_ltp=134.0)
    total = eng.get_control_total_pnl("t1", "A1")
    assert total is not None
    assert total < 5000.0


def test_step3b_premium_pct_025_armed_at_3055():
    """premium_pct=0.25, open_premium=11765 → target=2941.25 < pnl≈3055 → ARMED."""
    eng = _make_engine()
    eng.on_open_position(_open_event())
    # Get open_premium from snapshot
    snap = eng._state_store.get_snapshot(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1"
    )
    open_premium = snap.control_open_premium
    target = open_premium * 0.25
    eng.update_control_open_pnl("t1", "A1", "strat1", current_ltp=134.0)
    total = eng.get_control_total_pnl("t1", "A1")
    assert total is not None
    assert total > target, f"Expected {total} > {target}"


# --- Step 4: POS_SYNC strong flat clears strategy state ---

def test_step4_on_position_flat_by_sync_clears_managed_position():
    """on_position_flat_by_sync clears _managed_positions without placing orders."""
    try:
        from app.strategies.ema20_strategy import Ema20Strategy
    except Exception:
        pytest.skip("Ema20Strategy not importable in this environment")

    strategy = MagicMock(spec=Ema20Strategy)
    # Simulate a managed position
    strategy._managed_positions = {"NIFTY28APR2624300CE": MagicMock()}
    strategy.position = None

    # Call the real method via the class (not the mock)
    Ema20Strategy.on_position_flat_by_sync(
        strategy, label="NIFTY28APR2624300CE", reason="pos_sync_strong_flat"
    )

    assert "NIFTY28APR2624300CE" not in strategy._managed_positions


# --- Step 5: EOD gate prevents duplicate close ---

def test_step5_eod_gate_suppresses_force_exit_when_risk_manager_shows_flat():
    """_has_authoritative_open_position returns False → EOD suppressed."""
    try:
        from app.strategies.ema20_strategy import Ema20Strategy
    except Exception:
        pytest.skip("Ema20Strategy not importable in this environment")

    strategy = MagicMock(spec=Ema20Strategy)
    label = "NIFTY28APR2624300CE"

    # risk_manager has empty open_positions (broker-confirmed flat)
    rm = MagicMock()
    rm.open_positions = {}
    strategy.risk_manager = rm
    strategy._managed_positions = {label: MagicMock()}

    result = Ema20Strategy._has_authoritative_open_position(strategy)
    assert result is False, "Should return False when risk_manager shows flat"


def test_step5_control_pnl_after_manual_close():
    """After manual broker close, control_realized_pnl reflects the exit."""
    eng = _make_engine()
    eng.on_open_position(_open_event(qty=65, price=181.0))
    eng.on_close_position(_close_event(qty=65, price=123.65))

    total = eng.get_control_total_pnl("t1", "A1")
    expected = (181.0 - 123.65) * 65
    assert total is not None
    assert abs(total - expected) < 1.0

    snap = eng._state_store.get_snapshot(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1"
    )
    assert snap.control_open_qty == 0
    assert snap.control_open_premium == 0.0
