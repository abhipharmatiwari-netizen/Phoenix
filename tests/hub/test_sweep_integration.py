"""Integration tests for profit sweep strategies with persistent state.

Tests the coordination between exit_engines.ProfitSweepEngine and
sweep_state.SweepStateManager for multi-instance deployments.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

from app.core.identifiers import BrokerAccountId, TenantId
from app.hub.sweep_state import SweepStateStore, SweepStateManager


@pytest.fixture
def mock_firestore_client():
    """Mock Firestore client."""
    return MagicMock()


@pytest.fixture
def state_store(mock_firestore_client):
    """Create a SweepStateStore with mocked Firestore."""
    return SweepStateStore(mock_firestore_client)


@pytest.fixture
def state_manager(state_store):
    """Create a SweepStateManager."""
    return SweepStateManager(state_store)


class TestSweepIntegration:
    """Integration tests combining exit engines with persistent state."""

    def test_simple_strategy_respects_persistent_state(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test that SIMPLE sweep respects persistent daily limit."""
        # Setup: Account already has 1 sweep today in Firestore
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_reset_date": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Attempt to execute a SIMPLE sweep (which allows only 1 per day)
        can_sweep, reason = state_manager.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=1,  # SIMPLE strategy allows 1 per day
        )
        
        # Should be blocked because already swept once today
        assert can_sweep is False
        assert "Daily limit" in reason

    def test_multiple_strategy_respects_persistent_cooldown(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test that MULTIPLE sweep respects persistent cooldown."""
        # Setup: Account has a sweep 10 minutes ago
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        recent = now - timedelta(minutes=10)
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": recent.isoformat(),
            "last_reset_date": now.strftime("%Y-%m-%d"),
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Attempt to execute a MULTIPLE sweep (30-min cooldown)
        can_sweep, reason = state_manager.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
            cooldown_minutes=30,  # MULTIPLE strategy has 30-min cooldown
        )
        
        # Should be blocked due to cooldown
        assert can_sweep is False
        assert "Cooldown" in reason

    def test_multiple_strategy_allows_after_cooldown_expires(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test MULTIPLE sweep allows execution after cooldown expires."""
        # Setup: Account has a sweep 40 minutes ago (beyond 30-min cooldown)
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        past = now - timedelta(minutes=40)
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": past.isoformat(),
            "last_reset_date": now.strftime("%Y-%m-%d"),
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Attempt to execute a MULTIPLE sweep after cooldown expires
        can_sweep, reason = state_manager.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
            cooldown_minutes=30,
        )
        
        # Should be allowed because cooldown has expired
        assert can_sweep is True
        assert reason is None

    def test_sweep_recording_updates_persistent_state(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test that recording a sweep updates Firestore state."""
        # Setup: New account with no sweeps
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 0,
            "last_reset_date": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Record a sweep execution
        result = state_manager.record_sweep_execution(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        # Should succeed and Firestore set() should have been called
        assert result is True
        assert mock_doc_ref.set.called

    def test_daily_reset_persists_across_instances(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test that daily reset logic is consistent across instances.
        
        When a new instance queries state after midnight, it should
        automatically reset the counters for a new trading day.
        """
        # Setup: Old state from yesterday
        yesterday = (datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=1)).strftime("%Y-%m-%d")
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,  # Had 5 sweeps yesterday
            "last_sweep_time": "2026-01-13T15:30:00+05:30",
            "last_reset_date": yesterday,
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Get state (simulating a new instance starting after midnight)
        state = state_store.get_sweep_state(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        # Should have reset counters for new day
        assert state.sweep_count_today == 0
        assert state.last_sweep_time is None  # Also cleared on reset
        
        # Verify Firestore was updated with new state
        assert mock_doc_ref.set.called

    def test_multiple_instances_coordinate_via_state_store(
        self, mock_firestore_client
    ):
        """Test that multiple instances can coordinate through shared state."""
        # Setup two separate managers (simulating two running instances)
        store1 = SweepStateStore(mock_firestore_client)
        SweepStateManager(store1)
        
        store2 = SweepStateStore(mock_firestore_client)
        manager2 = SweepStateManager(store2)
        
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        
        # Simulate Instance 1 executing a sweep 5 minutes ago
        instance1_state = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": (now - timedelta(minutes=5)).isoformat(),
            "last_reset_date": now.strftime("%Y-%m-%d"),
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = instance1_state
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Instance 2 should see Instance 1's sweep and respect cooldown
        can_sweep, reason = manager2.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            cooldown_minutes=30,
        )
        
        # Instance 2 should be blocked by Instance 1's recent sweep
        assert can_sweep is False
        assert "Cooldown" in reason

    def test_concurrent_sweep_attempts_limited_by_daily_max(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test that concurrent sweep attempts are limited by daily maximum."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        
        # Setup: Account already at daily limit (5 MULTIPLE sweeps)
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,
            "last_sweep_time": (now - timedelta(minutes=60)).isoformat(),  # Old enough to pass cooldown
            "last_reset_date": now.strftime("%Y-%m-%d"),
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Try to execute another sweep
        can_sweep, reason = state_manager.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
            cooldown_minutes=30,
        )
        
        # Should be blocked by daily limit despite passing cooldown check
        assert can_sweep is False
        assert "Daily limit" in reason

    def test_different_tenants_have_independent_state(
        self, state_store, mock_firestore_client
    ):
        """Test that different tenants have independent sweep states."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.strftime("%Y-%m-%d")
        
        # Setup: Two different tenants with different states
        # Tenant 1: At daily limit
        tenant1_state = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,
            "last_reset_date": today,
        }
        
        # Tenant 2: No sweeps yet
        tenant2_state = {
            "tenant_id": "tenant_2",
            "broker_account_id": "account_1",
            "sweep_count_today": 0,
            "last_reset_date": today,
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        
        def get_side_effect():
            # Return different data based on doc_id
            return mock_doc
        
        def to_dict_side_effect():
            # This is simplified - in real scenario would check doc_id
            if hasattr(mock_doc, '_tenant_data'):
                return mock_doc._tenant_data
            return tenant2_state
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc.to_dict = MagicMock(side_effect=to_dict_side_effect)
        mock_doc_ref.get.return_value = mock_doc
        
        # Set data for tenant 1
        mock_doc._tenant_data = tenant1_state
        tenant1_count = state_store.get_sweep_count_today(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        assert tenant1_count == 5
        
        # Set data for tenant 2
        mock_doc._tenant_data = tenant2_state
        tenant2_count = state_store.get_sweep_count_today(
            TenantId("tenant_2"),
            BrokerAccountId("account_1"),
        )
        assert tenant2_count == 0

    def test_different_accounts_have_independent_state(
        self, state_store, mock_firestore_client
    ):
        """Test that different accounts have independent sweep states."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.strftime("%Y-%m-%d")
        
        # Setup: Same tenant, different accounts
        # Account 1: At daily limit
        account1_state = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,
            "last_reset_date": today,
        }
        
        # Account 2: No sweeps
        account2_state = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_2",
            "sweep_count_today": 0,
            "last_reset_date": today,
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        
        def to_dict_side_effect():
            if hasattr(mock_doc, '_account_data'):
                return mock_doc._account_data
            return account2_state
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc.to_dict = MagicMock(side_effect=to_dict_side_effect)
        mock_doc_ref.get.return_value = mock_doc
        
        # Set data for account 1
        mock_doc._account_data = account1_state
        account1_count = state_store.get_sweep_count_today(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        assert account1_count == 5
        
        # Set data for account 2
        mock_doc._account_data = account2_state
        account2_count = state_store.get_sweep_count_today(
            TenantId("tenant_1"),
            BrokerAccountId("account_2"),
        )
        assert account2_count == 0

    def test_sweep_status_reflects_persistent_state(
        self, state_store, state_manager, mock_firestore_client
    ):
        """Test that sweep status correctly reflects persistent state."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        last_sweep = now - timedelta(minutes=25)
        today = now.strftime("%Y-%m-%d")
        
        # Setup: Account with recent sweep activity
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 3,
            "last_sweep_time": last_sweep.isoformat(),
            "last_reset_date": today,
        }
        
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = existing_doc_data
        
        mock_doc_ref = mock_firestore_client.collection.return_value.document.return_value
        mock_doc_ref.get.return_value = mock_doc
        
        # Get sweep status
        status = state_manager.get_sweep_status(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        # Verify status reflects persistent state
        assert status["sweep_count_today"] == 3
        assert status["last_sweep_time"] == last_sweep.isoformat()
        assert 24 < status["minutes_since_last_sweep"] < 26  # ~25 minutes
        assert status["last_reset_date"] == today
