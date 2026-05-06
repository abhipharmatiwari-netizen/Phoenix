"""
Comprehensive tests for kill switch functionality.
"""

import datetime as dt
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def risk_manager_with_kill_switch(tmp_path):
    """Create a RiskManager with kill switch configuration."""
    mod = importlib.import_module("app.core.risk_manager")
    RiskManager = mod.RiskManager
    
    rm = RiskManager(
        instrument_meta={
            "NIFTY24MAYFUT": {"lot_size": 75},
            "BANKNIFTY24MAYFUT": {"lot_size": 25},
        },
        order_client=SimpleNamespace(),
        max_daily_loss=5000.0,  # Kill switch at -5000 loss
        max_intraday_drawdown=3000.0,  # Kill switch at 3000 drawdown
        kill_switch_square_off_open_positions=True,
        state_path=str(tmp_path / "risk_state.json"),
    )
    return rm


def test_kill_switch_not_activated_initially(risk_manager_with_kill_switch):
    """Test kill switch is not activated on initialization."""
    assert risk_manager_with_kill_switch.kill_switch_activated is False


def test_kill_switch_activates_on_max_daily_loss(risk_manager_with_kill_switch):
    """Test kill switch activates when max daily loss is exceeded."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Set up initial state
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    risk_manager_with_kill_switch.daily_peak_equity = 0.0
    risk_manager_with_kill_switch.daily_realized_pnl = 0.0
    
    # Trigger loss that exceeds max_daily_loss (-5000)
    risk_manager_with_kill_switch.daily_realized_pnl = -5500.0
    
    # Check kill switch
    risk_manager_with_kill_switch._check_kill_switch(now)
    
    assert risk_manager_with_kill_switch.kill_switch_activated is True
    assert risk_manager_with_kill_switch.kill_switch_date == now.date()


def test_kill_switch_activates_on_max_intraday_drawdown(risk_manager_with_kill_switch):
    """Test kill switch activates when max intraday drawdown is exceeded."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Set up state with drawdown
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    risk_manager_with_kill_switch.daily_peak_equity = 10000.0  # Peak equity reached
    risk_manager_with_kill_switch.daily_realized_pnl = 7200.0  # Drawdown of 2800
    
    # Increase drawdown to exceed limit (3000)
    risk_manager_with_kill_switch.daily_realized_pnl = 6800.0  # Drawdown now 3200
    
    # Check kill switch
    risk_manager_with_kill_switch._check_kill_switch(now)
    
    assert risk_manager_with_kill_switch.kill_switch_activated is True


def test_kill_switch_not_activated_below_limits(risk_manager_with_kill_switch):
    """Test kill switch is not activated when losses are below limits."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Set up state with acceptable losses (both loss and drawdown below limits)
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    risk_manager_with_kill_switch.daily_peak_equity = 10000.0
    risk_manager_with_kill_switch.daily_realized_pnl = 8000.0  # Profit, not loss
    
    # Check kill switch
    risk_manager_with_kill_switch._check_kill_switch(now)
    
    assert risk_manager_with_kill_switch.kill_switch_activated is False


def test_kill_switch_blocks_new_entries(risk_manager_with_kill_switch):
    """Test kill switch blocks new entries when activated."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Activate kill switch
    risk_manager_with_kill_switch.kill_switch_activated = True
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    
    # Try to place a new entry order (SELL side)
    decision = risk_manager_with_kill_switch.place_order(
        label="NIFTY24MAYFUT",
        side="SELL",
        qty=1,
        price=20000.0,
        trade_mode="LIVE",
    )
    
    # New entries should be blocked
    assert decision.allowed is False
    assert "kill_switch" in decision.reason.lower()


def test_kill_switch_allows_closing_orders(risk_manager_with_kill_switch):
    """Test kill switch allows closing orders (buy to close) even when active."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Set up: add an open position
    risk_manager_with_kill_switch.open_positions = {
        "NIFTY24MAYFUT": {"side": "SELL", "entry_price": 20000.0, "qty": 1}
    }
    
    # Activate kill switch
    risk_manager_with_kill_switch.kill_switch_activated = True
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    
    # Try to close the position with a BUY order
    decision = risk_manager_with_kill_switch.place_order(
        label="NIFTY24MAYFUT",
        side="BUY",
        qty=1,
        price=19900.0,
        trade_mode="LIVE",
    )
    
    # Closing orders should be allowed (may fail for other reasons but not kill switch)
    # The actual decision depends on other factors, but kill_switch reason should not appear
    assert "kill_switch" not in decision.reason.lower()


def test_kill_switch_resets_on_new_day(risk_manager_with_kill_switch):
    """Test kill switch is reset when a new day starts."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    today = dt.datetime.now(ist)
    tomorrow = today + dt.timedelta(days=1)
    
    # Activate kill switch on today
    risk_manager_with_kill_switch.kill_switch_date = today.date()
    risk_manager_with_kill_switch.kill_switch_activated = True
    risk_manager_with_kill_switch.daily_realized_pnl = -6000.0
    
    # Check if new day resets it
    risk_manager_with_kill_switch._reset_daily_if_new_day(tomorrow)
    
    assert risk_manager_with_kill_switch.kill_switch_activated is False
    assert risk_manager_with_kill_switch.kill_switch_date == tomorrow.date()
    assert risk_manager_with_kill_switch.daily_realized_pnl == 0.0


def test_kill_switch_state_persisted_to_file(risk_manager_with_kill_switch):
    """Test kill switch state is persisted to file."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Activate kill switch
    risk_manager_with_kill_switch.kill_switch_activated = True
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    
    # Persist state
    risk_manager_with_kill_switch._persist_state()
    
    # Read persisted state
    state_data = json.loads(
        risk_manager_with_kill_switch.state_path.read_text(encoding="utf-8")
    )
    
    assert state_data["kill_switch_activated"] is True
    assert state_data["kill_switch_date"] == now.date().isoformat()


def test_kill_switch_state_restored_from_file(tmp_path):
    """Test kill switch state is restored from file on initialization."""
    mod = importlib.import_module("app.core.risk_manager")
    RiskManager = mod.RiskManager
    
    state_file = tmp_path / "risk_state.json"
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    saved_date = dt.datetime.now(ist).date()
    
    # Create and save state
    state_data = {
        "realized_pnl": 0.0,
        "open_spreads": {},
        "open_positions": {},
        "max_equity": 0.0,
        "daily_realized_pnl": -6000.0,
        "daily_peak_equity": 0.0,
        "kill_switch_activated": True,
        "kill_switch_date": saved_date.isoformat(),
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")
    
    # Create manager and check if state is restored
    rm = RiskManager(
        instrument_meta={},
        order_client=SimpleNamespace(),
        state_path=str(state_file),
    )
    
    assert rm.kill_switch_activated is True
    assert rm.kill_switch_date == saved_date


@patch('app.core.risk_manager.RiskManager.square_off_all')
def test_kill_switch_squares_off_positions(mock_square_off, risk_manager_with_kill_switch):
    """Test kill switch calls square_off_all when activated with open positions."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    # Set up open positions
    risk_manager_with_kill_switch.open_positions = {
        "NIFTY24MAYFUT": {"side": "SELL", "entry_price": 20000.0, "qty": 1}
    }
    risk_manager_with_kill_switch.kill_switch_square_off_open_positions = True
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    
    # Trigger kill switch activation
    risk_manager_with_kill_switch.daily_realized_pnl = -5500.0
    risk_manager_with_kill_switch._check_kill_switch(now)
    
    # Verify square_off_all was called
    assert mock_square_off.called
    call_kwargs = mock_square_off.call_args[1]
    assert call_kwargs["tag"] == "KILL_SWITCH_EXIT"


def test_kill_switch_disabled_when_limits_are_zero(tmp_path):
    """Test kill switch is effectively disabled when limits are 0."""
    mod = importlib.import_module("app.core.risk_manager")
    RiskManager = mod.RiskManager
    
    rm = RiskManager(
        instrument_meta={},
        order_client=SimpleNamespace(),
        max_daily_loss=0.0,  # Disabled
        max_intraday_drawdown=0.0,  # Disabled
        state_path=str(tmp_path / "risk_state.json"),
    )
    
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    rm.kill_switch_date = now.date()
    
    # Even with massive loss, kill switch should not activate
    rm.daily_realized_pnl = -100000.0
    rm._check_kill_switch(now)
    
    assert rm.kill_switch_activated is False


def test_kill_switch_no_square_off_when_disabled(risk_manager_with_kill_switch):
    """Test kill switch doesn't square off when kill_switch_square_off_open_positions is False."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    risk_manager_with_kill_switch.kill_switch_square_off_open_positions = False
    risk_manager_with_kill_switch.open_positions = {
        "NIFTY24MAYFUT": {"side": "SELL", "entry_price": 20000.0, "qty": 1}
    }
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    
    # Trigger kill switch
    risk_manager_with_kill_switch.daily_realized_pnl = -5500.0
    
    with patch.object(
        risk_manager_with_kill_switch, 'square_off_all'
    ) as mock_square_off:
        risk_manager_with_kill_switch._check_kill_switch(now)
        
        # square_off_all should NOT be called
        assert not mock_square_off.called


def test_multiple_loss_events_single_kill_switch(risk_manager_with_kill_switch):
    """Test multiple loss events trigger kill switch only once."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    
    risk_manager_with_kill_switch.kill_switch_date = now.date()
    risk_manager_with_kill_switch.daily_peak_equity = 0.0
    
    # First trigger at -5500
    risk_manager_with_kill_switch.daily_realized_pnl = -5500.0
    risk_manager_with_kill_switch._check_kill_switch(now)
    first_activation_state = risk_manager_with_kill_switch.kill_switch_activated
    
    # Increase loss to -6000
    risk_manager_with_kill_switch.daily_realized_pnl = -6000.0
    with patch.object(risk_manager_with_kill_switch, 'square_off_all') as mock_sq:
        risk_manager_with_kill_switch._check_kill_switch(now)
        # Should not call square_off_all again (already activated)
        assert not mock_sq.called
    
    assert first_activation_state is True
    assert risk_manager_with_kill_switch.kill_switch_activated is True
