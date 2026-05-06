"""Tests for AngelBrokerClient.is_token_near_expiry / proactive_relogin_if_near_expiry (#79)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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


def test_returns_false_when_far_from_refresh_boundary():
    client = _make_client()
    # Logged in at 10:00 IST today = 04:30 UTC (after today's 08:00 IST boundary)
    # Now it's also 10:00 IST — next 08:00 IST is ~22 hours away
    utc_now = datetime(2026, 4, 24, 4, 30, 0, tzinfo=timezone.utc)
    client._logged_in_at = datetime(2026, 4, 24, 3, 30, 0, tzinfo=timezone.utc)  # 09:00 IST

    with patch("app.brokers.angel_client.datetime") as mock_dt:
        mock_dt.now.return_value = utc_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = client.is_token_near_expiry(margin_minutes=10)

    assert result is False


def test_returns_true_when_within_margin_of_refresh_boundary():
    client = _make_client()
    # 07:53 IST = 02:23 UTC — 08:00 IST boundary is 02:30 UTC, 7 min away → within 10 min margin
    # Logged in yesterday at 10:00 IST = yesterday 04:30 UTC (after yesterday's boundary)
    utc_now = datetime(2026, 4, 24, 2, 23, 0, tzinfo=timezone.utc)
    client._logged_in_at = datetime(2026, 4, 23, 4, 30, 0, tzinfo=timezone.utc)

    with patch("app.brokers.angel_client.datetime") as mock_dt:
        mock_dt.now.return_value = utc_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = client.is_token_near_expiry(margin_minutes=10)

    assert result is True


def test_returns_true_when_token_already_stale_after_8am():
    """Token issued before today's 08:00 IST but checked just after — already stale."""
    client = _make_client()
    # Logged in at April 27 10:00 IST = April 27 04:30 UTC (before today's 08:00 IST boundary)
    client._logged_in_at = datetime(2026, 4, 27, 4, 30, 0, tzinfo=timezone.utc)
    # Now it's April 28 08:05 IST = April 28 02:35 UTC (5 min after the 08:00 IST boundary)
    utc_now = datetime(2026, 4, 28, 2, 35, 0, tzinfo=timezone.utc)

    with patch("app.brokers.angel_client.datetime") as mock_dt:
        mock_dt.now.return_value = utc_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = client.is_token_near_expiry(margin_minutes=10)

    assert result is True


def test_returns_false_after_post_boundary_relogin():
    """After a successful post-08:00-IST relogin, token should not be flagged as near expiry."""
    client = _make_client()
    # Logged in at April 28 08:05 IST = April 28 02:35 UTC (after the 08:00 IST boundary)
    client._logged_in_at = datetime(2026, 4, 28, 2, 35, 0, tzinfo=timezone.utc)
    # Now it's April 28 08:10 IST = April 28 02:40 UTC
    utc_now = datetime(2026, 4, 28, 2, 40, 0, tzinfo=timezone.utc)

    with patch("app.brokers.angel_client.datetime") as mock_dt:
        mock_dt.now.return_value = utc_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = client.is_token_near_expiry(margin_minutes=10)

    # Next 08:00 IST boundary is ~23h50m away — should NOT trigger proactive relogin
    assert result is False


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
