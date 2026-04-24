"""Tests for OrderLifecycleService.force_terminal_positions_by_symbol_pattern (#73)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.orders.order_lifecycle import OrderLifecycleService


def _make_db_mock(rowcount):
    """Return a mock context-manager chain for connect_with_retry / cursor."""
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur

    cm = MagicMock()
    cm.__enter__ = lambda s: conn
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_force_terminal_positions_returns_rowcount():
    """force_terminal_positions_by_symbol_pattern returns the DB rowcount."""
    cm = _make_db_mock(5)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        result = OrderLifecycleService.force_terminal_positions_by_symbol_pattern(
            symbol_pattern="%MAR26%",
            min_age_days=45,
        )

    assert result == 5


def test_force_terminal_positions_zero_when_no_rows():
    """Returns 0 when no matching rows exist."""
    cm = _make_db_mock(0)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        result = OrderLifecycleService.force_terminal_positions_by_symbol_pattern(
            symbol_pattern="%APR27%",
            min_age_days=45,
        )

    assert result == 0


def test_force_terminal_positions_none_rowcount_returns_zero():
    """Handles None rowcount gracefully (some DB drivers return None for DML)."""
    cm = _make_db_mock(None)
    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        result = OrderLifecycleService.force_terminal_positions_by_symbol_pattern(
            symbol_pattern="%FEB26%",
            min_age_days=45,
        )

    assert result == 0


def test_force_terminal_positions_min_age_clamped_to_one():
    """min_age_days=0 is clamped to 1 to avoid accidentally touching same-day records."""
    captured_sql: list[str] = []

    cur = MagicMock()
    cur.rowcount = 0
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = lambda sql, params=None: captured_sql.append(str(sql))

    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__ = lambda s: conn
    cm.__exit__ = MagicMock(return_value=False)

    with (
        patch("app.data.postgres.get_control_plane_dsn", return_value="dsn://test"),
        patch("app.data.postgres.connect_with_retry", return_value=cm),
    ):
        OrderLifecycleService.force_terminal_positions_by_symbol_pattern(
            symbol_pattern="%MAR26%",
            min_age_days=0,
        )

    assert captured_sql, "execute was not called"
    assert "1 days" in captured_sql[0], (
        f"Expected '1 days' in SQL (clamped from 0), got: {captured_sql[0]}"
    )
