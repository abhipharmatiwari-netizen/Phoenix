"""Tests for MULTIPLE dual profit sweep strategy."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import Settings
from app.core.identifiers import BrokerAccountId, TenantId
from app.hub.exit_engines import ProfitSweepEngine
from app.brokers.base import ProductType, Position
from app.pnl.profit_lock import ProfitLockManager


@pytest.fixture
def mock_settings_multiple():
    """Create mock settings for MULTIPLE strategy."""
    settings = MagicMock(spec=Settings)
    settings.default_time_zone = "Asia/Kolkata"
    settings.enable_profit_checks = True
    settings.profit_enable_daily_target = True
    settings.profit_daily_target = 2000
    settings.profit_daily_target_paper = 2000
    
    # MULTIPLE strategy settings
    settings.profit_sweep_enabled = True
    settings.profit_sweep_strategy = "MULTIPLE"
    settings.profit_simple_enabled = False
    settings.profit_multiple_enabled = True
    settings.profit_multiple_target = 500
    settings.profit_sweep_cooldown_minutes = 30
    settings.profit_max_sweeps_per_day = 5
    settings.profit_lock_enabled = True
    settings.profit_lock_giveback_pct = 0.1
    settings.profit_lock_exit_cooldown_seconds = 0.0
    settings.profit_lock_require_signal = True
    
    return settings


@pytest.fixture
def mock_pnl_engine_multiple():
    """Create mock PnL engine for MULTIPLE tests."""
    engine = MagicMock()
    engine.get_current_total_pnl = MagicMock(return_value=500)
    # Return None so sweep falls back to cash PnL (no control data in these tests)
    engine.get_control_total_pnl = MagicMock(return_value=None)
    return engine


@pytest.fixture
def sweep_engine_multiple(mock_settings_multiple, mock_pnl_engine_multiple):
    """Create ProfitSweepEngine with MULTIPLE strategy."""
    from app.hub.sweep_state import SweepStateManager, SweepStateStore
    from unittest.mock import MagicMock
    
    mock_sweep_engine = MagicMock()
    mock_state_store = MagicMock()
    mock_order_router = MagicMock()
    mock_order_router.submit_order = AsyncMock(return_value=("order_123", MagicMock(status="PENDING")))
    
    # Create mock sweep state manager
    mock_firestore_store = MagicMock(spec=SweepStateStore)
    mock_firestore_store.can_sweep_now = MagicMock(return_value=(True, None))
    mock_firestore_store.record_sweep = MagicMock()
    mock_firestore_store.get_sweep_state = MagicMock(return_value=MagicMock(
        sweep_count_today=0,
        last_sweep_time=None,
        last_reset_date="2026-01-15"
    ))
    mock_firestore_store.get_minutes_since_last_sweep = MagicMock(return_value=None)
    sweep_state_manager = SweepStateManager(state_store=mock_firestore_store)
    
    profit_lock_manager = ProfitLockManager(
        default_time_zone=mock_settings_multiple.default_time_zone
    )
    engine = ProfitSweepEngine(
        settings=mock_settings_multiple,
        pnl_engine=mock_pnl_engine_multiple,
        sweep_engine=mock_sweep_engine,
        state_store=mock_state_store,
        order_router=mock_order_router,
        sweep_state_manager=sweep_state_manager,
        profit_lock_manager=profit_lock_manager,
    )
    engine._is_excluded_exchange = MagicMock(return_value=False)
    return engine


class TestMultipleSweepStrategy:
    """Test MULTIPLE dual profit sweep strategy."""

    @pytest.mark.asyncio
    async def test_multiple_sweep_executes_when_target_reached(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep executes when profit target is reached."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[position])
        
        await sweep_engine_multiple.maybe_sweep_multiple([runner])

        # Verify order was submitted
        assert sweep_engine_multiple.order_router.submit_order.called
        sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.assert_called_once()
        
        assert sweep_engine_multiple.sweep_state_manager.state_store.get_sweep_state.called

    @pytest.mark.asyncio
    async def test_multiple_sweep_respects_max_sweeps(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep respects max sweeps per day limit."""
        sweep_engine_multiple.settings.profit_max_sweeps_per_day = 2
        sweep_engine_multiple.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(False, "Daily limit reached (2 sweeps)")
        )
        
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[position])
        
        # Third sweep should be blocked
        await sweep_engine_multiple.maybe_sweep_multiple([runner])

        assert not sweep_engine_multiple.order_router.submit_order.called
        sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_sweep_respects_cooldown(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep respects cooldown between sweeps."""
        sweep_engine_multiple.settings.profit_sweep_cooldown_minutes = 10
        sweep_engine_multiple.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(False, "Cooldown active (5 min remaining)")
        )
        
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[position])
        
        call_count_before = sweep_engine_multiple.order_router.submit_order.call_count
        
        # Try to sweep (should fail cooldown check)
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        
        call_count_after = sweep_engine_multiple.order_router.submit_order.call_count

        # No new orders should be submitted
        assert call_count_after == call_count_before
        sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_sweep_proceeds_after_cooldown(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep proceeds after cooldown expires."""
        sweep_engine_multiple.settings.profit_sweep_cooldown_minutes = 10
        sweep_engine_multiple.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            return_value=(True, None)
        )
        
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[position])
        
        call_count_before = sweep_engine_multiple.order_router.submit_order.call_count
        
        # Try to sweep (should succeed)
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        
        call_count_after = sweep_engine_multiple.order_router.submit_order.call_count
        
        # New order should be submitted
        assert call_count_after > call_count_before
        sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.assert_called()

    @pytest.mark.asyncio
    async def test_multiple_sweep_skips_below_target(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep doesn't execute below profit target."""
        sweep_engine_multiple.pnl_engine.get_current_total_pnl = MagicMock(return_value=300)
        
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        call_count_before = sweep_engine_multiple.order_router.submit_order.call_count
        
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        
        call_count_after = sweep_engine_multiple.order_router.submit_order.call_count
        
        # No orders should be submitted
        assert call_count_after == call_count_before

    @pytest.mark.asyncio
    async def test_multiple_sweep_disabled_mode(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep is skipped when mode is disabled."""
        sweep_engine_multiple.settings.profit_multiple_enabled = False
        
        runner = MagicMock()
        runner.is_running = True
        
        call_count_before = sweep_engine_multiple.order_router.submit_order.call_count
        
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        
        call_count_after = sweep_engine_multiple.order_router.submit_order.call_count
        
        # No orders should be submitted
        assert call_count_after == call_count_before

    @pytest.mark.asyncio
    async def test_multiple_sweep_invalid_target(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep is skipped when target is invalid."""
        sweep_engine_multiple.settings.profit_multiple_target = None
        
        runner = MagicMock()
        runner.is_running = True
        
        call_count_before = sweep_engine_multiple.order_router.submit_order.call_count
        
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        
        call_count_after = sweep_engine_multiple.order_router.submit_order.call_count
        
        # No orders should be submitted
        assert call_count_after == call_count_before

    @pytest.mark.asyncio
    async def test_multiple_sweep_no_positions(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep handles no positions gracefully."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[])
        
        call_count_before = sweep_engine_multiple.order_router.submit_order.call_count
        
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        
        call_count_after = sweep_engine_multiple.order_router.submit_order.call_count
        
        # No orders should be submitted
        assert call_count_after == call_count_before

    @pytest.mark.asyncio
    async def test_multiple_sweep_increments_counter(self, sweep_engine_multiple):
        """Test that multiple sweeps increment the sweep counter correctly."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[position])
        
        # First sweep
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        assert sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.call_count == 1
        
        # Second sweep
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        assert sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.call_count == 2
        
        # Third sweep
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        assert sweep_engine_multiple.sweep_state_manager.state_store.record_sweep.call_count == 3

    @pytest.mark.asyncio
    async def test_multiple_sweep_tracks_last_sweep_time(self, sweep_engine_multiple):
        """Test that MULTIPLE sweep properly tracks last sweep time."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine_multiple.state_store.get_positions = MagicMock(return_value=[position])
        
        await sweep_engine_multiple.maybe_sweep_multiple([runner])
        sweep_engine_multiple.sweep_state_manager.state_store.get_sweep_state.assert_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
