"""
Comprehensive tests for profit sweeping functionality.
"""

import pytest
from typing import Optional
from unittest.mock import Mock, patch

from app.pnl.pnl_engine import PnLEngine
from app.pnl.profit_engine import ProfitEngine, ProfitConfig, ProfitDecision
from app.pnl.types import TradeEvent, PnLSnapshot, PnLSnapshotKey
from app.core.identifiers import TenantId, BrokerAccountId, StrategyId


@pytest.fixture
def pnl_engine():
    """Create a PnLEngine for testing."""
    return PnLEngine()


@pytest.fixture
def profit_engine(pnl_engine):
    """Create a ProfitEngine with the PnLEngine."""
    return ProfitEngine(pnl_engine)


@pytest.fixture
def sample_ids():
    """Standard IDs for testing."""
    return {
        "tenant_id": TenantId("tenant-001"),
        "broker_account_id": BrokerAccountId("account-001"),
        "strategy_id": StrategyId("strategy-001"),
    }


class TestProfitConfigGeneration:
    """Tests for profit target configuration generation."""

    @patch('app.pnl.profit_engine.get_settings')
    def test_config_disabled_when_feature_off(self, mock_settings, profit_engine, sample_ids):
        """Test config is disabled when profit target feature is off."""
        mock_settings.return_value.profit_enable_daily_target = False
        
        config = profit_engine._get_config_for(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert config.enable_daily_target is False
        assert config.daily_target is None

    @patch('app.pnl.profit_engine.get_settings')
    def test_config_uses_live_target(self, mock_settings, profit_engine, sample_ids):
        """Test config uses live trading target when not in paper mode."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = 10000.0
        
        config = profit_engine._get_config_for(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert config.enable_daily_target is True
        assert config.daily_target == 50000.0

    @patch('app.pnl.profit_engine.get_settings')
    def test_config_uses_paper_target_when_available(self, mock_settings, profit_engine, sample_ids):
        """Test config uses paper target when in paper mode and override is set."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = 10000.0
        
        config = profit_engine._get_config_for(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=True,
        )
        
        assert config.enable_daily_target is True
        assert config.daily_target == 10000.0

    @patch('app.pnl.profit_engine.get_settings')
    def test_config_falls_back_to_live_when_paper_not_set(self, mock_settings, profit_engine, sample_ids):
        """Test config falls back to live target when paper target is not set."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        config = profit_engine._get_config_for(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=True,
        )
        
        assert config.enable_daily_target is True
        assert config.daily_target == 50000.0

    @patch('app.pnl.profit_engine.get_settings')
    def test_config_disabled_when_target_not_set(self, mock_settings, profit_engine, sample_ids):
        """Test config is disabled when target is not configured."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = None
        mock_settings.return_value.profit_daily_target_paper = None
        
        config = profit_engine._get_config_for(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert config.enable_daily_target is False
        assert config.daily_target is None


class TestSweepDecision:
    """Tests for profit sweep decision logic."""

    @patch('app.pnl.profit_engine.get_settings')
    def test_no_sweep_when_feature_disabled(self, mock_settings, profit_engine, sample_ids):
        """Test no sweep is triggered when feature is disabled."""
        mock_settings.return_value.profit_enable_daily_target = False
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert decision.should_sweep is False
        assert decision.reason == "profit_target_disabled"
        assert decision.target is None

    @patch('app.pnl.profit_engine.get_settings')
    def test_no_sweep_when_no_pnl_snapshot(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test no sweep when PnL snapshot doesn't exist yet."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert decision.should_sweep is False
        assert decision.reason == "no_pnl_snapshot"
        assert decision.target == 50000.0

    @patch('app.pnl.profit_engine.get_settings')
    def test_no_sweep_below_target(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test no sweep when realized PnL is below target."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        # Set up a snapshot with profit below target
        pnl_engine.update_unrealized_and_exposure(
            sample_ids["tenant_id"],
            sample_ids["broker_account_id"],
            sample_ids["strategy_id"],
            unrealized_pnl=10000.0,
            gross_exposure=100000.0,
        )
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert decision.should_sweep is False
        assert decision.reason == "below_target"
        assert decision.current_total_pnl == 10000.0
        assert decision.target == 50000.0

    @patch('app.pnl.profit_engine.get_settings')
    def test_sweep_triggered_at_target(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test sweep is triggered when realized PnL equals target."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        # Create a trade that brings realized PnL to exactly 50000
        trade_event = TradeEvent(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            symbol="NIFTY50",
            qty=-1,  # Sell
            price=50000.0,
            trade_time=None,
            fees=0.0,
        )
        pnl_engine.on_trade(trade_event)
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert decision.should_sweep is True
        assert decision.reason == "daily_target_reached"
        assert decision.current_total_pnl == 50000.0
        assert decision.target == 50000.0

    @patch('app.pnl.profit_engine.get_settings')
    def test_sweep_triggered_above_target(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test sweep is triggered when realized PnL exceeds target."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        # Create a trade with profit above target
        trade_event = TradeEvent(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            symbol="NIFTY50",
            qty=-1,  # Sell
            price=60000.0,
            trade_time=None,
            fees=500.0,
        )
        pnl_engine.on_trade(trade_event)
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert decision.should_sweep is True
        assert decision.reason == "daily_target_reached"
        assert decision.current_total_pnl == 59500.0  # 60000 - 500 fees
        assert decision.target == 50000.0


class TestProfitSweepPaperMode:
    """Tests for paper mode specific behavior."""

    @patch('app.pnl.profit_engine.get_settings')
    def test_paper_mode_uses_paper_target(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test paper mode uses paper-specific target."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = 5000.0
        
        # Create trade with 5000 profit (hits paper target)
        trade_event = TradeEvent(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            symbol="NIFTY50",
            qty=-1,
            price=5000.0,
            trade_time=None,
            fees=0.0,
        )
        pnl_engine.on_trade(trade_event)
        
        # Check paper mode sweep decision
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=True,
        )
        
        assert decision.should_sweep is True
        assert decision.target == 5000.0


class TestMultipleAccountProfit:
    """Tests for profit tracking across multiple accounts."""

    @patch('app.pnl.profit_engine.get_settings')
    def test_separate_profit_tracking_per_account(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test profit is tracked separately for different accounts."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 10000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        account1 = sample_ids["broker_account_id"]
        account2 = BrokerAccountId("account-002")
        
        # Add profit to account 1
        trade1 = TradeEvent(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=account1,
            strategy_id=sample_ids["strategy_id"],
            symbol="NIFTY50",
            qty=-1,
            price=15000.0,
            trade_time=None,
            fees=0.0,
        )
        pnl_engine.on_trade(trade1)
        
        # Add profit to account 2
        trade2 = TradeEvent(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=account2,
            strategy_id=sample_ids["strategy_id"],
            symbol="NIFTY50",
            qty=-1,
            price=5000.0,
            trade_time=None,
            fees=0.0,
        )
        pnl_engine.on_trade(trade2)
        
        # Check decisions
        decision1 = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=account1,
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        decision2 = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=account2,
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        # Account 1 should trigger sweep (15000 > 10000)
        assert decision1.should_sweep is True
        assert decision1.current_total_pnl == 15000.0
        
        # Account 2 should not trigger sweep (5000 < 10000)
        assert decision2.should_sweep is False
        assert decision2.current_total_pnl == 5000.0


class TestProfitDecisionDetails:
    """Tests for detailed profit decision information."""

    @patch('app.pnl.profit_engine.get_settings')
    def test_decision_includes_current_pnl(self, mock_settings, profit_engine, pnl_engine, sample_ids):
        """Test decision includes current realized PnL amount."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 50000.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        # Add some profit
        trade = TradeEvent(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            symbol="NIFTY50",
            qty=-1,
            price=30000.0,
            trade_time=None,
            fees=1000.0,
        )
        pnl_engine.on_trade(trade)
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        assert decision.current_total_pnl == 29000.0  # 30000 - 1000 fees
        assert decision.target == 50000.0
        assert decision.reason == "below_target"

    @patch('app.pnl.profit_engine.get_settings')
    def test_decision_with_zero_target(self, mock_settings, profit_engine, sample_ids):
        """Test decision when target is set to zero (disabled)."""
        mock_settings.return_value.profit_enable_daily_target = True
        mock_settings.return_value.profit_daily_target = 0.0
        mock_settings.return_value.profit_daily_target_paper = None
        
        decision = profit_engine.check_should_sweep(
            tenant_id=sample_ids["tenant_id"],
            broker_account_id=sample_ids["broker_account_id"],
            strategy_id=sample_ids["strategy_id"],
            is_paper=False,
        )
        
        # Zero target should disable the feature
        assert decision.should_sweep is False
        assert decision.reason == "profit_target_disabled"
