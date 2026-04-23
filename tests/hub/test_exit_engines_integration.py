"""
Integration tests for exit_engines with persistent sweep state.
Tests interaction between ProfitSweepEngine and SweepStateManager.
"""

import pytest
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.config.settings import Settings
from app.core.identifiers import TenantId, BrokerAccountId, StrategyId
from app.brokers.base import Position, OrderSide, OrderStatus, ProductType
from app.data.state_store import StateStore
from app.pnl.pnl_engine import PnLEngine
from app.pnl.profit_engine import ProfitEngine as SweepProfitEngine
from app.pnl.profit_lock import ProfitLockManager
from app.orders.router import OrderRouter
from app.hub.account_runner import AccountRunner
from app.hub.exit_engines import ProfitSweepEngine
from app.hub.sweep_state import (
    SweepState,
    SweepStateManager,
    SweepStateStore,
)


@pytest.fixture
def settings():
    """Create test settings mock with dual sweep enabled."""
    settings = MagicMock(spec=Settings)
    settings.broker_name = "NSE"
    settings.default_time_zone = "Asia/Kolkata"
    settings.profit_sweep_enabled = True
    settings.profit_sweep_strategy = "SIMPLE"
    settings.profit_daily_target = 2000
    settings.profit_multiple_enabled = True
    settings.profit_multiple_target = 500
    settings.profit_sweep_cooldown_minutes = 30
    settings.profit_max_sweeps_per_day = 5
    settings.enable_paper_trading = True
    settings.profit_lock_enabled = False
    settings.profit_lock_giveback_pct = 0.1
    settings.profit_lock_exit_cooldown_seconds = 0.0
    settings.profit_lock_require_signal = True
    return settings


@pytest.fixture
def mock_pnl_engine():
    """Create mock PnL engine."""
    engine = MagicMock(spec=PnLEngine)
    engine.get_current_total_pnl = MagicMock(return_value=0.0)
    # Return None so sweep falls back to cash PnL (no control data configured in these tests)
    engine.get_control_total_pnl = MagicMock(return_value=None)
    return engine


@pytest.fixture
def mock_sweep_engine():
    """Create mock sweep profit engine."""
    return MagicMock(spec=SweepProfitEngine)


@pytest.fixture
def mock_state_store():
    """Create mock state store."""
    store = MagicMock(spec=StateStore)
    store.get_positions = MagicMock(return_value=[])
    return store


@pytest.fixture
def mock_order_router():
    """Create mock order router."""
    router = AsyncMock(spec=OrderRouter)
    router.place_order = AsyncMock(return_value=MagicMock())
    return router


@pytest.fixture
def mock_firestore_store():
    """Create mock Firestore state store."""
    store = MagicMock(spec=SweepStateStore)
    
    # Default sweep state
    default_state = SweepState(
        tenant_id="test_tenant",
        broker_account_id="test_account",
        sweep_count_today=0,
        last_sweep_time=None,
        last_reset_date="2026-01-15",
    )
    
    store.get_sweep_state = MagicMock(return_value=default_state)
    store.record_sweep = MagicMock()
    store.can_sweep_now = MagicMock(return_value=(True, None))
    store.get_minutes_since_last_sweep = MagicMock(return_value=None)
    return store


@pytest.fixture
def sweep_state_manager(mock_firestore_store):
    """Create SweepStateManager with mock Firestore store."""
    manager = SweepStateManager(state_store=mock_firestore_store)
    return manager


@pytest.fixture
def profit_sweep_engine(
    settings,
    mock_pnl_engine,
    mock_sweep_engine,
    mock_state_store,
    mock_order_router,
    sweep_state_manager,
):
    """Create ProfitSweepEngine with all dependencies."""
    profit_lock_manager = ProfitLockManager(
        default_time_zone=settings.default_time_zone
    )
    engine = ProfitSweepEngine(
        settings=settings,
        pnl_engine=mock_pnl_engine,
        sweep_engine=mock_sweep_engine,
        state_store=mock_state_store,
        order_router=mock_order_router,
        sweep_state_manager=sweep_state_manager,
        profit_lock_manager=profit_lock_manager,
    )
    
    # Mock methods that would be called during execution
    engine._is_excluded_exchange = MagicMock(return_value=False)
    engine._exit_all_positions = AsyncMock(return_value=0)
    
    return engine


@pytest.fixture
def mock_runner():
    """Create mock AccountRunner."""
    runner = MagicMock(spec=AccountRunner)
    runner.tenant_id = TenantId("test_tenant")
    runner.broker_account_id = BrokerAccountId("test_account")
    runner.is_running = True
    runner.runtime_mode = "LIVE"
    return runner


def create_position(
    symbol: str = "INFY",
    quantity: int = 1,
    entry_price: float = 2000.0,
) -> Position:
    """Create a test position."""
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_price=entry_price,
        product_type=ProductType.INTRADAY,
        entry_ts=None,
    )


# ============================================================================
# SIMPLE STRATEGY INTEGRATION TESTS
# ============================================================================


class TestSimpleStrategyIntegration:
    """Test SIMPLE strategy with persistent state."""

    @pytest.mark.asyncio
    async def test_simple_sweep_blocks_when_at_limit(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
    ):
        """Test that SIMPLE strategy blocks when at daily limit (1)."""
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(False, "Maximum sweeps reached for today")
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = 1500.0
        mock_state_store.get_positions.return_value = [create_position()]
        
        await profit_sweep_engine.maybe_sweep_simple([mock_runner])
        
        # Should not record sweep when blocked
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_simple_sweep_executes_on_profit_target(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test SIMPLE sweep executes when profit target reached."""
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        # Set realized PnL above SIMPLE target
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_daily_target + 500
        )
        position = create_position()
        mock_state_store.get_positions.return_value = [position]
        
        await profit_sweep_engine.maybe_sweep_simple([mock_runner])
        
        # Verify sweep was recorded
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_simple_sweep_skips_when_target_not_reached(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test SIMPLE sweep skipped when profit target not reached."""
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        # Set realized PnL below SIMPLE target
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_daily_target - 200
        )
        mock_state_store.get_positions.return_value = [create_position()]
        
        await profit_sweep_engine.maybe_sweep_simple([mock_runner])
        
        # Should not record sweep
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_simple_sweep_records_even_with_no_positions(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test SIMPLE sweep records state even when no positions exist."""
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_daily_target + 100
        )
        mock_state_store.get_positions.return_value = []  # No positions
        
        await profit_sweep_engine.maybe_sweep_simple([mock_runner])
        
        # Should still record sweep (to enforce daily limit)
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()


# ============================================================================
# MULTIPLE STRATEGY INTEGRATION TESTS
# ============================================================================


class TestMultipleStrategyIntegration:
    """Test MULTIPLE strategy with persistent state."""

    @pytest.mark.asyncio
    async def test_multiple_sweep_respects_max_per_day(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test MULTIPLE strategy respects daily sweep limit."""
        # Setup: already at max sweeps
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(False, "Maximum 5 sweeps per day reached")
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_multiple_target + 200
        )
        mock_state_store.get_positions.return_value = [create_position()]
        
        # Change settings to MULTIPLE
        profit_sweep_engine.settings.profit_sweep_strategy = "MULTIPLE"
        
        await profit_sweep_engine.maybe_sweep_multiple([mock_runner])
        
        # Should not execute sweep (at limit)
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_sweep_enforces_cooldown(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test MULTIPLE strategy enforces cooldown between sweeps."""
        # Setup: last sweep within cooldown period
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(False, "Cooldown period active (10/30 minutes)")
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_multiple_target + 200
        )
        mock_state_store.get_positions.return_value = [create_position()]
        
        # Change settings to MULTIPLE
        profit_sweep_engine.settings.profit_sweep_strategy = "MULTIPLE"
        
        await profit_sweep_engine.maybe_sweep_multiple([mock_runner])
        
        # Should not execute due to cooldown
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_sweep_executes_after_cooldown(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test MULTIPLE sweep executes after cooldown expires."""
        # Setup: cooldown expired
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_multiple_target + 200
        )
        mock_state_store.get_positions.return_value = [create_position()]
        
        # Change settings to MULTIPLE
        profit_sweep_engine.settings.profit_sweep_strategy = "MULTIPLE"
        
        await profit_sweep_engine.maybe_sweep_multiple([mock_runner])
        
        # Should execute (cooldown expired)
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_increments_sweep_count(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
        settings,
    ):
        """Test MULTIPLE strategy increments sweep count after execution."""
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = (
            settings.profit_multiple_target + 100
        )
        mock_state_store.get_positions.return_value = [create_position()]
        
        # Change settings to MULTIPLE
        profit_sweep_engine.settings.profit_sweep_strategy = "MULTIPLE"
        
        await profit_sweep_engine.maybe_sweep_multiple([mock_runner])
        
        # Verify record_sweep was called (increments count in state)
        profit_sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()


# ============================================================================
# DISABLED/EDGE CASE TESTS
# ============================================================================


class TestDisabledAndEdgeCases:
    """Test disabled strategies and edge cases."""

    @pytest.mark.asyncio
    async def test_sweep_disabled_skips_both_strategies(
        self,
        profit_sweep_engine,
        mock_runner,
        mock_pnl_engine,
        mock_state_store,
    ):
        """Test both strategies skip when sweep disabled."""
        profit_sweep_engine.settings.profit_sweep_enabled = False
        mock_pnl_engine.get_current_total_pnl.return_value = 2000.0
        mock_state_store.get_positions.return_value = [create_position()]
        
        # Try SIMPLE
        profit_sweep_engine.settings.profit_sweep_strategy = "SIMPLE"
        await profit_sweep_engine.maybe_sweep_simple([mock_runner])
        assert not profit_sweep_engine.sweep_state_manager.state_store.record_sweep.called
        
        # Try MULTIPLE
        profit_sweep_engine.settings.profit_sweep_strategy = "MULTIPLE"
        await profit_sweep_engine.maybe_sweep_multiple([mock_runner])
        assert not profit_sweep_engine.sweep_state_manager.state_store.record_sweep.called

    @pytest.mark.asyncio
    async def test_empty_runner_list(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
    ):
        """Test empty runner list is handled gracefully."""
        mock_pnl_engine.get_current_total_pnl.return_value = 2000.0
        mock_state_store.get_positions.return_value = [create_position()]
        
        # Should handle empty list without error
        await profit_sweep_engine.maybe_sweep_simple([])
        await profit_sweep_engine.maybe_sweep_multiple([])
        
        assert not profit_sweep_engine.sweep_state_manager.state_store.record_sweep.called

    @pytest.mark.asyncio
    async def test_null_pnl_result(
        self,
        profit_sweep_engine,
        mock_pnl_engine,
        mock_state_store,
        mock_runner,
    ):
        """Test null PnL result is handled safely."""
        profit_sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        mock_pnl_engine.get_current_total_pnl.return_value = None
        
        # Should skip gracefully
        await profit_sweep_engine.maybe_sweep_simple([mock_runner])
        assert not profit_sweep_engine.sweep_state_manager.state_store.record_sweep.called

    @pytest.mark.asyncio
    async def test_negative_profit_target(
        self,
        profit_sweep_engine,
        mock_runner,
    ):
        """Test invalid (negative) profit targets are rejected."""
        profit_sweep_engine.settings.profit_multiple_target = -100
        
        # Should skip due to validation
        await profit_sweep_engine.maybe_sweep_multiple([mock_runner])
        assert not profit_sweep_engine.sweep_state_manager.state_store.record_sweep.called
