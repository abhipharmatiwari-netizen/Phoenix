"""Tests for ProfitSweepEngine dual sweep strategies."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config.settings import Settings
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.hub.exit_engines import ProfitSweepEngine
from app.brokers.base import OrderSide, ProductType, OrderPurpose, Position
from app.pnl.profit_lock import ProfitLockManager


@pytest.fixture
def mock_settings():
    """Create mock settings with dual sweep configuration."""
    settings = MagicMock(spec=Settings)
    settings.default_time_zone = "Asia/Kolkata"
    settings.enable_profit_checks = True
    settings.profit_enable_daily_target = True
    settings.profit_daily_target = 2000
    settings.profit_daily_target_paper = 2000
    
    # Dual sweep settings
    settings.profit_sweep_enabled = True
    settings.profit_sweep_strategy = "SIMPLE"
    settings.profit_simple_enabled = True
    settings.profit_multiple_enabled = False
    settings.profit_daily_target = 2000
    settings.profit_multiple_target = 500
    settings.profit_sweep_cooldown_minutes = 30
    settings.profit_max_sweeps_per_day = 5
    settings.profit_lock_enabled = True
    settings.profit_lock_giveback_pct = 0.1
    settings.profit_lock_exit_cooldown_seconds = 0.0
    settings.profit_lock_require_signal = True
    
    return settings


@pytest.fixture
def mock_pnl_engine():
    """Create mock PnL engine."""
    engine = MagicMock()
    engine.get_current_total_pnl = MagicMock(return_value=2000)
    return engine


@pytest.fixture
def mock_sweep_engine():
    """Create mock sweep engine."""
    engine = MagicMock()
    return engine


@pytest.fixture
def mock_state_store():
    """Create mock state store."""
    store = MagicMock()
    return store


@pytest.fixture
def mock_order_router():
    """Create mock order router."""
    router = MagicMock()
    router.submit_order = AsyncMock(return_value=("order_123", MagicMock(status="PENDING")))
    return router


@pytest.fixture
def profit_lock_manager(mock_settings):
    """Create profit lock manager."""
    return ProfitLockManager(default_time_zone=mock_settings.default_time_zone)


@pytest.fixture
def sweep_engine(
    mock_settings,
    mock_pnl_engine,
    mock_sweep_engine,
    mock_state_store,
    mock_order_router,
    profit_lock_manager,
):
    """Create ProfitSweepEngine instance."""
    from app.hub.sweep_state import SweepStateManager, SweepStateStore
    from unittest.mock import MagicMock
    
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
    
    engine = ProfitSweepEngine(
        settings=mock_settings,
        pnl_engine=mock_pnl_engine,
        sweep_engine=mock_sweep_engine,
        state_store=mock_state_store,
        order_router=mock_order_router,
        sweep_state_manager=sweep_state_manager,
        profit_lock_manager=profit_lock_manager,
    )
    engine._is_excluded_exchange = MagicMock(return_value=False)
    return engine


class TestDualSweepConfiguration:
    """Test dual sweep configuration checks."""

    def test_dual_sweep_enabled_simple(self, sweep_engine):
        """Test that SIMPLE strategy is correctly identified as enabled."""
        assert sweep_engine._dual_sweep_enabled() is True

    def test_dual_sweep_disabled_when_feature_off(self, sweep_engine):
        """Test that dual sweep is disabled when feature flag is off."""
        sweep_engine.settings.profit_sweep_enabled = False
        assert sweep_engine._dual_sweep_enabled() is False

    def test_dual_sweep_invalid_strategy(self, sweep_engine):
        """Test that invalid strategy is treated as disabled."""
        sweep_engine.settings.profit_sweep_strategy = "INVALID"
        assert sweep_engine._dual_sweep_enabled() is False


class TestSimpleSweepStrategy:
    """Test SIMPLE dual profit sweep strategy."""

    @pytest.mark.asyncio
    async def test_simple_sweep_holds_positions_at_target(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep holds positions when lock is active and signal valid."""
        # Setup
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
        
        sweep_engine.state_store.get_positions = MagicMock(return_value=[position])
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(return_value=2000)
        sweep_engine.state_store.get_strategy_signal_valid = MagicMock(return_value=True)
        
        # Execute
        await sweep_engine.maybe_sweep_simple([runner])

        # Verify no sweep while lock holds
        assert not mock_order_router.submit_order.called
        sweep_engine.sweep_state_manager.state_store.record_sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_simple_sweep_exits_on_giveback(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep exits when total PnL falls below lock floor."""
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

        sweep_engine.state_store.get_positions = MagicMock(return_value=[position])
        sweep_engine.state_store.get_strategy_signal_valid = MagicMock(return_value=True)
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(
            side_effect=[2200, 1950]
        )

        await sweep_engine.maybe_sweep_simple([runner])
        assert not mock_order_router.submit_order.called

        await sweep_engine.maybe_sweep_simple([runner])
        assert mock_order_router.submit_order.called
        sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_simple_sweep_exits_on_signal_invalid(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep exits when the strategy signal becomes invalid."""
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

        sweep_engine.state_store.get_positions = MagicMock(return_value=[position])
        sweep_engine.state_store.get_strategy_signal_valid = MagicMock(return_value=False)
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(return_value=2100)

        await sweep_engine.maybe_sweep_simple([runner])

        assert mock_order_router.submit_order.called
        sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()
        call_args = mock_order_router.submit_order.call_args
        order_req = call_args.kwargs["order_req"]
        assert order_req.symbol == "SBIN"
        assert order_req.quantity == 100
        assert order_req.side == OrderSide.SELL
        assert order_req.purpose == OrderPurpose.EXIT

    @pytest.mark.asyncio
    async def test_simple_sweep_skips_below_target(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep doesn't execute when target not reached."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(return_value=500)
        
        await sweep_engine.maybe_sweep_simple([runner])
        
        # Should not submit order
        assert not mock_order_router.submit_order.called

    @pytest.mark.asyncio
    async def test_simple_sweep_disabled_mode(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep is skipped when mode is disabled."""
        sweep_engine.settings.profit_simple_enabled = False
        
        runner = MagicMock()
        runner.is_running = True
        
        await sweep_engine.maybe_sweep_simple([runner])
        
        # Should not submit order
        assert not mock_order_router.submit_order.called

    @pytest.mark.asyncio
    async def test_simple_sweep_invalid_target(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep is skipped when target is invalid."""
        sweep_engine.settings.profit_daily_target = None
        
        runner = MagicMock()
        runner.is_running = True
        
        await sweep_engine.maybe_sweep_simple([runner])
        
        # Should not submit order
        assert not mock_order_router.submit_order.called

    @pytest.mark.asyncio
    async def test_simple_sweep_once_per_day(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep only executes once per trading day."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        sweep_engine.sweep_state_manager.state_store.can_sweep_now = MagicMock(
            side_effect=[(True, None), (False, "Daily limit reached (1 sweep)")]
        )
        
        position = MagicMock(spec=Position)
        position.symbol = "SBIN"
        position.quantity = 100
        position.exchange = "NSE"
        position.symbol_token = "123456"
        position.product_type = ProductType.INTRADAY
        
        sweep_engine.state_store.get_positions = MagicMock(return_value=[position])
        sweep_engine.state_store.get_strategy_signal_valid = MagicMock(return_value=False)
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(return_value=2100)
        
        # First call
        await sweep_engine.maybe_sweep_simple([runner])
        first_call_count = mock_order_router.submit_order.call_count
        
        # Second call (same day)
        await sweep_engine.maybe_sweep_simple([runner])
        second_call_count = mock_order_router.submit_order.call_count
        
        # Should not have called submit_order again
        assert first_call_count == second_call_count

    @pytest.mark.asyncio
    async def test_simple_sweep_no_positions(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep handles no positions gracefully."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        sweep_engine.state_store.get_positions = MagicMock(return_value=[])
        sweep_engine.state_store.get_strategy_signal_valid = MagicMock(return_value=False)
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(return_value=2100)
        
        await sweep_engine.maybe_sweep_simple([runner])
        
        # Should not submit order when no positions
        assert not mock_order_router.submit_order.called
        sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()

    @pytest.mark.asyncio
    async def test_simple_sweep_mixed_positions(self, sweep_engine, mock_order_router):
        """Test that SIMPLE sweep handles both long and short positions."""
        runner = MagicMock()
        runner.is_running = True
        runner.tenant_id = TenantId("tenant_1")
        runner.broker_account_id = BrokerAccountId("account_1")
        runner.runtime_mode = "LIVE"
        
        # Long position
        long_position = MagicMock(spec=Position)
        long_position.symbol = "SBIN"
        long_position.quantity = 100
        long_position.exchange = "NSE"
        long_position.symbol_token = "123456"
        long_position.product_type = ProductType.INTRADAY
        
        # Short position
        short_position = MagicMock(spec=Position)
        short_position.symbol = "INFY"
        short_position.quantity = -50
        short_position.exchange = "NSE"
        short_position.symbol_token = "234567"
        short_position.product_type = ProductType.INTRADAY
        
        sweep_engine.state_store.get_positions = MagicMock(return_value=[long_position, short_position])
        sweep_engine.state_store.get_strategy_signal_valid = MagicMock(return_value=False)
        sweep_engine.pnl_engine.get_current_total_pnl = MagicMock(return_value=2100)
        
        await sweep_engine.maybe_sweep_simple([runner])

        # Should submit orders for both positions
        assert mock_order_router.submit_order.call_count == 2
        sweep_engine.sweep_state_manager.state_store.record_sweep.assert_called_once()
        
        # Check the calls
        calls = mock_order_router.submit_order.call_args_list
        long_order = calls[0].kwargs["order_req"]
        short_order = calls[1].kwargs["order_req"]
        
        assert long_order.side == OrderSide.SELL  # Sell long
        assert short_order.side == OrderSide.BUY  # Buy short


class TestExitAllPositionsHelper:
    """Test the _exit_all_positions helper method."""

    @pytest.mark.asyncio
    async def test_exit_all_positions_returns_count(self, sweep_engine):
        """Test that _exit_all_positions returns count of successful exits."""
        position1 = MagicMock(spec=Position)
        position1.symbol = "SBIN"
        position1.quantity = 100
        position1.exchange = "NSE"
        position1.symbol_token = "123456"
        position1.product_type = ProductType.INTRADAY
        
        position2 = MagicMock(spec=Position)
        position2.symbol = "INFY"
        position2.quantity = -50
        position2.exchange = "NSE"
        position2.symbol_token = "234567"
        position2.product_type = ProductType.INTRADAY
        
        sweep_engine.order_router.submit_order = AsyncMock(
            return_value=("order_123", MagicMock(status="PENDING"))
        )
        
        count = await sweep_engine._exit_all_positions(
            tenant_id=TenantId("tenant_1"),
            broker_account_id=BrokerAccountId("account_1"),
            strategy_id=StrategyId("test_strategy"),
            positions=[position1, position2],
            reason="TEST",
        )
        
        assert count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
