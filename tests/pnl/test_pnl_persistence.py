"""
Test real-time PnL persistence to BigQuery.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import TradeEvent
from app.core.identifiers import TenantId, BrokerAccountId, StrategyId


@pytest.fixture
def pnl_engine():
    """Create a PnLEngine instance for testing."""
    return PnLEngine()


@pytest.fixture
def sample_trade_event():
    """Create a sample trade event for testing."""
    return TradeEvent(
        tenant_id=TenantId("tenant-001"),
        broker_account_id=BrokerAccountId("account-001"),
        strategy_id=StrategyId("strategy-001"),
        symbol="NIFTY50",
        qty=1,
        price=100.0,
        trade_time=datetime.now(timezone.utc),
        fees=5.0,
    )


def test_on_trade_updates_realized_pnl(pnl_engine, sample_trade_event):
    """Test that on_trade correctly updates realized PnL."""
    pnl_engine.on_trade(sample_trade_event)
    
    snapshot = pnl_engine.get_snapshot(
        sample_trade_event.tenant_id,
        sample_trade_event.broker_account_id,
        sample_trade_event.strategy_id,
    )
    
    assert snapshot is not None
    # Realized PnL = -(qty * price) - fees = -(1 * 100) - 5 = -105
    assert snapshot.realized_pnl == -105.0
    assert snapshot.as_of == sample_trade_event.trade_time


@patch('app.pnl.pnl_engine.insert_daily_pnl_snapshot')
def test_on_trade_persists_to_bigquery(mock_insert, pnl_engine, sample_trade_event):
    """Test that on_trade calls persist_snapshot which inserts to BigQuery."""
    pnl_engine.on_trade(sample_trade_event)
    
    # Verify insert_daily_pnl_snapshot was called
    assert mock_insert.called
    
    # Verify the record structure
    call_args = mock_insert.call_args[0][0]
    assert call_args["tenant_id"] == "tenant-001"
    assert call_args["broker_account_id"] == "account-001"
    assert call_args["strategy_id"] == "strategy-001"
    assert call_args["realized_pnl"] == -105.0
    assert "timestamp" in call_args
    assert "date" in call_args


@patch('app.pnl.pnl_engine.insert_daily_pnl_snapshot')
def test_update_unrealized_and_exposure_persists(mock_insert, pnl_engine, sample_trade_event):
    """Test that update_unrealized_and_exposure persists to BigQuery."""
    pnl_engine.update_unrealized_and_exposure(
        sample_trade_event.tenant_id,
        sample_trade_event.broker_account_id,
        sample_trade_event.strategy_id,
        unrealized_pnl=150.0,
        gross_exposure=50000.0,
    )
    
    # Verify insert_daily_pnl_snapshot was called
    assert mock_insert.called
    
    # Verify the record
    call_args = mock_insert.call_args[0][0]
    assert call_args["unrealized_pnl"] == 150.0
    assert call_args["gross_exposure"] == 50000.0
    assert call_args["total_pnl"] == 150.0  # realized_pnl defaults to 0


@patch('app.pnl.pnl_engine.insert_daily_pnl_snapshot')
def test_persistence_handles_errors_gracefully(mock_insert, pnl_engine, sample_trade_event):
    """Test that persistence errors don't interrupt trading."""
    # Make insert_daily_pnl_snapshot raise an exception
    mock_insert.side_effect = Exception("BigQuery connection failed")
    
    # This should not raise an exception, errors should be logged
    pnl_engine.on_trade(sample_trade_event)
    
    # The snapshot should still be updated in memory
    snapshot = pnl_engine.get_snapshot(
        sample_trade_event.tenant_id,
        sample_trade_event.broker_account_id,
        sample_trade_event.strategy_id,
    )
    assert snapshot is not None
    assert snapshot.realized_pnl == -105.0


@patch('app.pnl.pnl_engine.insert_daily_pnl_snapshot')
def test_multiple_trades_persist_sequentially(mock_insert, pnl_engine):
    """Test that multiple trades are persisted separately."""
    event1 = TradeEvent(
        tenant_id=TenantId("tenant-001"),
        broker_account_id=BrokerAccountId("account-001"),
        strategy_id=StrategyId("strategy-001"),
        symbol="NIFTY50",
        qty=1,
        price=100.0,
        trade_time=datetime(2026, 1, 9, 10, 0, 0, tzinfo=timezone.utc),
        fees=5.0,
    )
    
    event2 = TradeEvent(
        tenant_id=TenantId("tenant-001"),
        broker_account_id=BrokerAccountId("account-001"),
        strategy_id=StrategyId("strategy-001"),
        symbol="NIFTY50",
        qty=-1,
        price=110.0,
        trade_time=datetime(2026, 1, 9, 11, 0, 0, tzinfo=timezone.utc),
        fees=5.0,
    )
    
    pnl_engine.on_trade(event1)
    pnl_engine.on_trade(event2)
    
    # Verify both were persisted
    assert mock_insert.call_count == 2
    
    # Check the cumulative PnL
    snapshot = pnl_engine.get_snapshot(
        event1.tenant_id,
        event1.broker_account_id,
        event1.strategy_id,
    )
    # First trade: -(1 * 100) - 5 = -105
    # Second trade: -(-1 * 110) - 5 = 110 - 5 = 105
    # Total: -105 + 105 = 0
    assert snapshot.realized_pnl == 0.0
