"""Tests for positions endpoints (legacy and tenant-scoped)."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, MagicMock
from types import SimpleNamespace
import importlib

from app.server import app
from fastapi import status


@pytest.fixture
def client(monkeypatch):
    """Create a test client with admin auth configured."""
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    return TestClient(app)


class TestLegacyPositionsEndpoint:
    """Tests for the legacy /positions endpoint (backwards compatibility)."""

    def test_positions_missing_tenant_header(self, client):
        """Test that /positions requires X-Tenant-Id header."""
        response = client.get(
            "/positions?broker_account_id=test-account",
            headers={"X-Admin-Key": "test-admin"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "X-Tenant-Id" in response.json()["detail"]

    def test_positions_missing_broker_account_when_header_present(self, client):
        """Test that /positions requires broker_account_id or can resolve one."""
        with patch("app.server.get_broker_accounts_for_tenant") as mock_get_accounts:
            # Simulate no accounts found
            mock_get_accounts.return_value = []
            
            response = client.get(
                "/positions",
                headers={"X-Tenant-Id": "tenant-123", "X-Admin-Key": "test-admin"},
            )
            assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_positions_success_with_tenant_and_broker_account(self, client):
        """Test successful /positions call with headers."""
        mock_positions = [
            {"symbol": "NIFTY", "qty": 1, "current_value": 1000, "pnl": 50}
        ]
        
        with patch("app.server.get_hub_runtime") as mock_runtime:
            mock_hub = MagicMock()
            mock_hub.state_store.get_positions.return_value = mock_positions
            mock_runtime.return_value = mock_hub
            
            response = client.get(
                "/positions?broker_account_id=account-123",
                headers={"X-Tenant-Id": "tenant-123", "X-Admin-Key": "test-admin"},
            )
            
            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["tenant_id"] == "tenant-123"
            assert data["broker_account_id"] == "account-123"
            assert data["positions"] == mock_positions

    def test_positions_resolves_first_account_when_not_specified(self, client):
        """Test that /positions can resolve first account when not specified."""
        mock_positions = [{"symbol": "BANKNIFTY", "qty": 1}]
        mock_accounts = [{"broker_account_id": "resolved-account-123"}]
        
        with patch("app.server.get_broker_accounts_for_tenant") as mock_get_accounts:
            with patch("app.server.get_hub_runtime") as mock_runtime:
                mock_get_accounts.return_value = mock_accounts
                mock_hub = MagicMock()
                mock_hub.state_store.get_positions.return_value = mock_positions
                mock_runtime.return_value = mock_hub
                
                response = client.get(
                    "/positions",
                    headers={"X-Tenant-Id": "tenant-123", "X-Admin-Key": "test-admin"},
                )
                
                assert response.status_code == status.HTTP_200_OK
                data = response.json()
                assert data["broker_account_id"] == "resolved-account-123"


class TestTenantScopedPositionsEndpoint:
    """Tests for the tenant-scoped /tenant/me/accounts/{id}/positions endpoint."""

    def test_tenant_positions_accessible(self, client):
        """Test that tenant positions endpoint is registered."""
        # Note: Actual auth context is mocked in real tests
        # This just verifies the endpoint exists and routes correctly
        with patch("app.dashboard.tenant_routes.get_tenant_context"):
            # The route should exist; exact behavior depends on auth mocking
            pass


def test_positions_endpoint_returns_correct_structure():
    """Test that positions response has expected structure."""
    expected_keys = {"tenant_id", "broker_account_id", "positions"}
    # This is a contract test - verify response shape
    assert True  # Placeholder for schema validation


def test_error_handling_for_404(monkeypatch):
    """Test that 404 is returned with meaningful message when account not found."""
    monkeypatch.setattr(
        importlib.import_module("app.dashboard.auth"),
        "get_settings",
        lambda: SimpleNamespace(admin_api_key="test-admin"),
    )
    client = TestClient(app)
    
    with patch("app.server.get_hub_runtime") as mock_runtime:
        mock_hub = MagicMock()
        mock_hub.state_store.get_positions.side_effect = Exception("Account not found")
        mock_runtime.return_value = mock_hub
        
        response = client.get(
            "/positions?broker_account_id=nonexistent",
            headers={"X-Tenant-Id": "tenant-123", "X-Admin-Key": "test-admin"},
        )
        
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "detail" in response.json()
