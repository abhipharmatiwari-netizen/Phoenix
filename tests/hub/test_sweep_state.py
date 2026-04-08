"""Tests for persistent sweep state management."""

import pytest
from unittest.mock import MagicMock, PropertyMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.identifiers import BrokerAccountId, TenantId
from app.hub.sweep_state import SweepState, SweepStateStore, SweepStateManager


@pytest.fixture
def mock_firestore_doc():
    """Create mock Firestore document."""
    doc = MagicMock()
    doc.exists = False
    doc.to_dict.return_value = {}
    return doc


@pytest.fixture
def mock_firestore_client():
    """Create mock Firestore client with proper chaining support."""
    client = MagicMock()
    
    # Create a mock that supports chaining: client.collection().document().get()
    collection = MagicMock()
    doc_ref = MagicMock()
    
    # Default behavior for new accounts (document doesn't exist)
    default_doc = MagicMock(exists=False)
    doc_ref.get.return_value = default_doc
    doc_ref.set.return_value = None
    
    collection.document.return_value = doc_ref
    client.collection.return_value = collection
    
    return client


@pytest.fixture
def state_store(mock_firestore_client):
    """Create SweepStateStore with mock Firestore."""
    return SweepStateStore(
        firestore_client=mock_firestore_client,
        default_time_zone="Asia/Kolkata",
    )


@pytest.fixture
def state_manager(state_store):
    """Create SweepStateManager."""
    return SweepStateManager(state_store)


class TestSweepState:
    """Test SweepState data class."""

    def test_sweep_state_creation(self):
        """Test creating a SweepState instance."""
        state = SweepState(
            tenant_id="tenant_1",
            broker_account_id="account_1",
            sweep_count_today=2,
        )
        assert state.tenant_id == "tenant_1"
        assert state.broker_account_id == "account_1"
        assert state.sweep_count_today == 2

    def test_sweep_state_to_firestore(self):
        """Test converting SweepState to Firestore format."""
        state = SweepState(
            tenant_id="tenant_1",
            broker_account_id="account_1",
            sweep_count_today=1,
            last_sweep_time="2026-01-14T10:30:00",
        )
        firestore_doc = state.to_firestore()
        
        assert firestore_doc["tenant_id"] == "tenant_1"
        assert firestore_doc["broker_account_id"] == "account_1"
        assert firestore_doc["sweep_count_today"] == 1
        assert firestore_doc["last_sweep_time"] == "2026-01-14T10:30:00"
        assert "updated_at" in firestore_doc

    def test_sweep_state_from_firestore(self):
        """Test creating SweepState from Firestore document."""
        doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 2,
            "last_sweep_time": "2026-01-14T10:30:00",
            "last_reset_date": "2026-01-14",
        }
        state = SweepState.from_firestore(doc_data)
        
        assert state.tenant_id == "tenant_1"
        assert state.sweep_count_today == 2
        assert state.last_sweep_time == "2026-01-14T10:30:00"


class TestSweepStateStore:
    """Test SweepStateStore persistence operations."""
    
    def _setup_mock_document(self, mock_firestore_client, doc_data):
        """Helper to configure mock for a specific document state."""
        mock_doc = MagicMock()
        if doc_data is None:
            mock_doc.exists = False
        else:
            mock_doc.exists = True
            mock_doc.to_dict.return_value = doc_data
        
        # Reset and reconfigure the mock chain
        mock_firestore_client.reset_mock()
        collection = MagicMock()
        doc_ref = MagicMock()
        doc_ref.get.return_value = mock_doc
        doc_ref.set.return_value = None
        
        collection.document.return_value = doc_ref
        mock_firestore_client.collection.return_value = collection
        
        return mock_doc

    def test_get_sweep_state_new_account(self, state_store, mock_firestore_client):
        """Test getting sweep state for new account creates default state."""
        self._setup_mock_document(mock_firestore_client, None)
        
        state = state_store.get_sweep_state(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert state.sweep_count_today == 0
        assert state.last_sweep_time is None

    def test_get_sweep_state_existing_account(self, state_store, mock_firestore_client):
        """Test getting sweep state for existing account."""
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 2,
            "last_sweep_time": "2026-01-14T10:30:00",
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        state = state_store.get_sweep_state(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert state.sweep_count_today == 2
        assert state.last_sweep_time == "2026-01-14T10:30:00"

    def test_daily_reset_on_new_day(self, state_store, mock_firestore_client):
        """Test that sweep count resets on new trading day."""
        yesterday = (datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=1)).strftime("%Y-%m-%d")
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,
            "last_reset_date": yesterday,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        state = state_store.get_sweep_state(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        # Should reset to 0 for new day
        assert state.sweep_count_today == 0

    def test_record_sweep(self, state_store, mock_firestore_client):
        """Test recording a sweep execution."""
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_reset_date": "2026-01-14",
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        result = state_store.record_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert result is True

    def test_get_sweep_count_today(self, state_store, mock_firestore_client):
        """Test getting current sweep count."""
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 3,
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        count = state_store.get_sweep_count_today(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert count == 3

    def test_get_last_sweep_time(self, state_store, mock_firestore_client):
        """Test getting last sweep timestamp."""
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        sweep_time = "2026-01-14T10:30:00+05:30"
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": sweep_time,
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        last_time = state_store.get_last_sweep_time(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert last_time is not None
        assert isinstance(last_time, datetime)

    def test_get_minutes_since_last_sweep(self, state_store, mock_firestore_client):
        """Test getting minutes elapsed since last sweep."""
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        past = now - timedelta(minutes=15)
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": past.isoformat(),
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        minutes = state_store.get_minutes_since_last_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert minutes is not None
        assert 14 < minutes < 16  # Allow for small timing variations

    def test_can_sweep_now_within_limit(self, state_store, mock_firestore_client):
        """Test can_sweep_now when within limits."""
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 2,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        can_sweep, reason = state_store.can_sweep_now(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
            cooldown_minutes=30,
        )
        
        assert can_sweep is True
        assert reason is None

    def test_can_sweep_now_exceeds_daily_limit(self, state_store, mock_firestore_client):
        """Test can_sweep_now when daily limit reached."""
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        can_sweep, reason = state_store.can_sweep_now(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
        )
        
        assert can_sweep is False
        assert "Daily limit" in reason

    def test_can_sweep_now_within_cooldown(self, state_store, mock_firestore_client):
        """Test can_sweep_now when in cooldown period."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        recent = now - timedelta(minutes=10)  # 10 minutes ago
        today = now.strftime("%Y-%m-%d")
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": recent.isoformat(),
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        can_sweep, reason = state_store.can_sweep_now(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            cooldown_minutes=30,
        )
        
        assert can_sweep is False
        assert "Cooldown" in reason

    def test_can_sweep_now_after_cooldown(self, state_store, mock_firestore_client):
        """Test can_sweep_now after cooldown expires."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        past = now - timedelta(minutes=40)  # 40 minutes ago
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
            "last_sweep_time": past.isoformat(),
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        can_sweep, reason = state_store.can_sweep_now(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            cooldown_minutes=30,
        )
        
        assert can_sweep is True
        assert reason is None

    def test_reset_sweep_state(self, state_store, mock_firestore_client):
        """Test manually resetting sweep state."""
        self._setup_mock_document(mock_firestore_client, None)
        
        result = state_store.reset_sweep_state(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert result is True


class TestSweepStateManager:
    """Test high-level SweepStateManager operations."""
    
    def _setup_mock_document(self, mock_firestore_client, doc_data):
        """Helper to configure mock for a specific document state."""
        mock_doc = MagicMock()
        if doc_data is None:
            mock_doc.exists = False
        else:
            mock_doc.exists = True
            mock_doc.to_dict.return_value = doc_data
        
        # Reset and reconfigure the mock chain
        mock_firestore_client.reset_mock()
        collection = MagicMock()
        doc_ref = MagicMock()
        doc_ref.get.return_value = mock_doc
        doc_ref.set.return_value = None
        
        collection.document.return_value = doc_ref
        mock_firestore_client.collection.return_value = collection
        
        return mock_doc

    def test_try_execute_sweep_allowed(self, state_manager, mock_firestore_client):
        """Test try_execute_sweep when conditions allow."""
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        can_sweep, reason = state_manager.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
            cooldown_minutes=30,
        )
        
        assert can_sweep is True
        assert reason is None

    def test_try_execute_sweep_blocked(self, state_manager, mock_firestore_client):
        """Test try_execute_sweep when blocked."""
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 5,
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        can_sweep, reason = state_manager.try_execute_sweep(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
            max_sweeps_per_day=5,
        )
        
        assert can_sweep is False
        assert reason is not None

    def test_record_sweep_execution(self, state_manager, mock_firestore_client):
        """Test recording sweep execution."""
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 1,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        result = state_manager.record_sweep_execution(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert result is True

    def test_get_sweep_status(self, state_manager, mock_firestore_client):
        """Test getting sweep status for display."""
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        past = now - timedelta(minutes=20)
        today = now.strftime("%Y-%m-%d")
        
        existing_doc_data = {
            "tenant_id": "tenant_1",
            "broker_account_id": "account_1",
            "sweep_count_today": 2,
            "last_sweep_time": past.isoformat(),
            "last_reset_date": today,
        }
        
        self._setup_mock_document(mock_firestore_client, existing_doc_data)
        
        status = state_manager.get_sweep_status(
            TenantId("tenant_1"),
            BrokerAccountId("account_1"),
        )
        
        assert status["sweep_count_today"] == 2
        assert status["last_sweep_time"] is not None
        assert status["minutes_since_last_sweep"] is not None
        assert status["last_reset_date"] == today


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
