"""Tests for AngelBrokerClient.is_token_near_expiry / proactive_relogin_if_near_expiry (#79)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.brokers.angel_client import AngelBrokerClient


def _make_client():
    """Return a minimally-wired AngelBrokerClient for testing."""
    secrets = MagicMock()
    secrets.client_local_ip = "10.0.0.1"
    secrets.client_public_ip = "1.2.3.4"
    secrets.mac_address = "aa:bb:cc:dd:ee:ff"
    secrets.api_key = "test-api-key"
    account = MagicMock()
    account.tenant_id = "t1"
    account.broker_account_id = "A1"
    client = AngelBrokerClient.__new__(AngelBrokerClient)
    client._account = account
    client._secrets = secrets
    client._order_client = MagicMock()
    client._login_lock = asyncio.Lock()
    client._logged_in_at = None
    client._broker_account_id = "A1"
    client._tenant_id = "t1"
    return client


# ---------------------------------------------------------------------------
# is_token_near_expiry
# ---------------------------------------------------------------------------

def test_returns_false_when_never_logged_in():
    client = _make_client()
    client._logged_in_at = None
    assert client.is_token_near_expiry(margin_minutes=10) is False


def test_returns_false_when_far_from_midnight():
    client = _make_client()
    # Simulate login at 09:00 IST — midnight is 14.5 hours away
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist = datetime(2026, 4, 24, 9, 0, 0, tzinfo=timezone.utc)
    client._logged_in_at = now_ist - ist_offset  # UTC equivalent

    with patch("app.brokers.angel_client.datetime") as mock_dt:
        # 09:00 IST → far from midnight
        utc_time = now_ist
        mock_dt.now.return_value = utc_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = client.is_token_near_expiry(margin_minutes=10)

    assert result is False


def test_returns_true_when_within_margin_of_midnight():
    client = _make_client()
    ist_offset = timedelta(hours=5, minutes=30)
    # 23:53 IST = 18:23 UTC — midnight IST is 18:30 UTC, 7 min away → within 10 min margin
    utc_now = datetime(2026, 4, 24, 18, 23, 0, tzinfo=timezone.utc)
    client._logged_in_at = utc_now - timedelta(hours=14)

    with patch("app.brokers.angel_client.datetime") as mock_dt:
        mock_dt.now.return_value = utc_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = client.is_token_near_expiry(margin_minutes=10)

    assert result is True


# ---------------------------------------------------------------------------
# proactive_relogin_if_near_expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_relogin_skips_when_not_near_expiry():
    client = _make_client()
    client._logged_in_at = None  # never logged in → is_token_near_expiry returns False

    result = await client.proactive_relogin_if_near_expiry(margin_minutes=10)
    assert result is False


@pytest.mark.asyncio
async def test_proactive_relogin_triggers_and_returns_true_when_near_expiry():
    client = _make_client()
    # Force is_token_near_expiry to return True
    client.is_token_near_expiry = MagicMock(return_value=True)
    client._login = AsyncMock()

    result = await client.proactive_relogin_if_near_expiry(margin_minutes=10)

    assert result is True
    client._login.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_proactive_relogin_returns_false_on_login_failure():
    client = _make_client()
    client.is_token_near_expiry = MagicMock(return_value=True)
    client._login = AsyncMock(side_effect=RuntimeError("TOTP failed"))

    result = await client.proactive_relogin_if_near_expiry(margin_minutes=10)

    assert result is False
