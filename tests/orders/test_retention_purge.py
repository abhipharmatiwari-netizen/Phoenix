"""Tests for data-retention purge functions (#73 / retention policy).

Covers:
- purge_old_terminal_outbox_records  (order_outbox.py)
- OrderLifecycleService.purge_old_flat_position_records  (order_lifecycle.py)
- purge_old_processed_markers  (trade_processed_store.py)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_mock(rowcount):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__ = lambda s: conn
    cm.__exit__ = MagicMock(return_value=False)
    return cm, cur


# ---------------------------------------------------------------------------
# purge_old_terminal_outbox_records
# ---------------------------------------------------------------------------

def test_purge_terminal_outbox_returns_rowcount():
    cm, cur = _make_db_mock(42)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_outbox import purge_old_terminal_outbox_records
        result = purge_old_terminal_outbox_records(retain_days=90)
    assert result == 42


def test_purge_terminal_outbox_returns_zero_when_none_deleted():
    cm, cur = _make_db_mock(0)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_outbox import purge_old_terminal_outbox_records
        result = purge_old_terminal_outbox_records(retain_days=90)
    assert result == 0


def test_purge_terminal_outbox_sql_contains_terminal_states():
    captured = []
    cm, cur = _make_db_mock(0)
    cur.execute = lambda sql, *a, **kw: captured.append(str(sql))
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_outbox import purge_old_terminal_outbox_records
        purge_old_terminal_outbox_records(retain_days=90)
    assert captured, "execute was not called"
    sql = captured[0]
    assert "TERMINAL_FILL" in sql
    assert "TERMINAL_NON_FILL" in sql
    assert "90 days" in sql


def test_purge_terminal_outbox_min_age_clamped_to_one():
    captured = []
    cm, cur = _make_db_mock(0)
    cur.execute = lambda sql, *a, **kw: captured.append(str(sql))
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_outbox import purge_old_terminal_outbox_records
        purge_old_terminal_outbox_records(retain_days=0)
    assert "1 days" in captured[0]


# ---------------------------------------------------------------------------
# OrderLifecycleService.purge_old_flat_position_records
# ---------------------------------------------------------------------------

def test_purge_flat_position_records_returns_rowcount():
    cm, cur = _make_db_mock(7)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_lifecycle import OrderLifecycleService
        result = OrderLifecycleService.purge_old_flat_position_records(retain_days=90)
    assert result == 7


def test_purge_flat_position_records_sql_targets_flat_none():
    captured = []
    cm, cur = _make_db_mock(0)
    cur.execute = lambda sql, *a, **kw: captured.append(str(sql))
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_lifecycle import OrderLifecycleService
        OrderLifecycleService.purge_old_flat_position_records(retain_days=90)
    assert captured
    sql = captured[0]
    assert "FLAT" in sql
    assert "NONE" in sql
    assert "90 days" in sql
    # Must NOT delete non-terminal states
    assert "OPEN" not in sql
    assert "RECONCILING" not in sql


def test_purge_flat_position_records_returns_zero_on_none_rowcount():
    cm, cur = _make_db_mock(None)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        from app.orders.order_lifecycle import OrderLifecycleService
        result = OrderLifecycleService.purge_old_flat_position_records(retain_days=90)
    assert result == 0


# ---------------------------------------------------------------------------
# purge_old_processed_markers
# ---------------------------------------------------------------------------

def test_purge_processed_markers_returns_rowcount():
    cm, cur = _make_db_mock(3)
    settings_mock = MagicMock()
    with (
        patch("app.orders.trade_processed_store.get_settings", return_value=settings_mock),
        patch("app.orders.trade_processed_store.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.orders.trade_processed_store.connect_with_retry", return_value=cm),
    ):
        from app.orders.trade_processed_store import purge_old_processed_markers
        result = purge_old_processed_markers(retain_days=180)
    assert result == 3


def test_purge_processed_markers_sql_uses_processed_at():
    captured = []
    cm, cur = _make_db_mock(0)
    cur.execute = lambda sql, *a, **kw: captured.append(str(sql))
    settings_mock = MagicMock()
    with (
        patch("app.orders.trade_processed_store.get_settings", return_value=settings_mock),
        patch("app.orders.trade_processed_store.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.orders.trade_processed_store.connect_with_retry", return_value=cm),
    ):
        from app.orders.trade_processed_store import purge_old_processed_markers
        purge_old_processed_markers(retain_days=180)
    assert captured
    sql = captured[0]
    assert "trade_processed_markers" in sql
    assert "processed_at" in sql
    assert "180 days" in sql


def test_purge_processed_markers_returns_zero_on_exception():
    with (
        patch("app.orders.trade_processed_store.get_settings", side_effect=RuntimeError("no db")),
    ):
        from app.orders.trade_processed_store import purge_old_processed_markers
        result = purge_old_processed_markers(retain_days=180)
    assert result == 0
