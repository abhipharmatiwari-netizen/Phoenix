"""Issue #204: dashboard avg_price=0 defence in _normalize_position_view.

The 2026-05-08 NIFTY24250CE incident showed Phoenix's Positions page
rendering Unrealized = ₹9,828 (= LTP 151.20 × qty 65) on a row whose
avg_price was 0 -- the symptom of corrupted internal_position_records
upstream. The dashboard's old formula `(ltp - avg_price) * qty` happily
booked the contract's full notional as paper profit when avg_price=0.

These tests pin the defence: when qty != 0 and avg_price <= 0 on a
position row, _normalize_position_view returns unrealized_pnl=None and
emits a structured POSITION_VIEW_AVG_PRICE_INVALID WARNING for ops
visibility instead of producing fake unrealised gain.
"""

from __future__ import annotations

import pytest

from app.dashboard import tenant_routes


@pytest.fixture(autouse=True)
def _stub_dashboard_bus_ltp(monkeypatch):
    """Force LTP lookup to return a known value (151.20 — the incident LTP)."""
    monkeypatch.setattr(
        tenant_routes.dashboard_bus,
        "get_last_price_for_instrument",
        lambda *, symbol=None, token=None: 151.20,
    )
    monkeypatch.setattr(
        tenant_routes.dashboard_bus,
        "get_last_price",
        lambda symbol: 151.20,
    )


def test_avg_price_zero_open_row_returns_no_unrealized_and_alerts(caplog):
    """The 2026-05-08 NIFTY24250CE replay: qty != 0, avg_price = 0,
    ltp present. Old code returned 9828.0 (= 151.20 * 65). New code
    must return None and emit the WARNING."""
    pos = {
        "symbol": "NIFTY12MAY2624250CE",
        "quantity": 65,
        "avg_price": 0.0,
        "product_type": "INTRADAY",
    }
    with caplog.at_level("WARNING"):
        view = tenant_routes._normalize_position_view(pos)

    assert view["unrealized_pnl"] is None, (
        "Unrealized PnL must NOT be computed when avg_price <= 0 on an "
        "OPEN row (would render LTP*qty as fake profit)."
    )
    assert view["quantity"] == 65
    assert view["avg_price"] == 0.0
    assert view["ltp"] == 151.20
    # Structured ALERT emitted.
    assert any(
        "POSITION_VIEW_AVG_PRICE_INVALID" in rec.getMessage()
        for rec in caplog.records
    ), "Expected POSITION_VIEW_AVG_PRICE_INVALID structured warning"


def test_avg_price_negative_open_row_also_blocked():
    """Defence covers any non-positive avg_price, not just exactly 0."""
    pos = {
        "symbol": "NIFTY12MAY2624250CE",
        "quantity": 65,
        "avg_price": -0.01,
        "product_type": "INTRADAY",
    }
    view = tenant_routes._normalize_position_view(pos)
    assert view["unrealized_pnl"] is None


def test_zero_qty_row_does_not_alert_even_with_zero_avg_price(caplog):
    """A flat row (qty=0) with avg_price=0 is normal end-state -- no
    ALERT, unrealized is 0 from (ltp - 0) * 0 = 0. The defence only
    fires on OPEN rows (qty != 0) because that's where the desync
    matters."""
    pos = {
        "symbol": "NIFTY12MAY2624250CE",
        "quantity": 0,
        "avg_price": 0.0,
        "product_type": "INTRADAY",
    }
    with caplog.at_level("WARNING"):
        view = tenant_routes._normalize_position_view(pos)

    assert view["unrealized_pnl"] == 0.0  # (151.20 - 0) * 0 = 0
    assert view["quantity"] == 0
    assert not any(
        "POSITION_VIEW_AVG_PRICE_INVALID" in rec.getMessage()
        for rec in caplog.records
    )


def test_valid_avg_price_unchanged_behaviour():
    """Sanity: the defence does not regress the normal path."""
    pos = {
        "symbol": "NIFTY12MAY2624250CE",
        "quantity": 65,
        "avg_price": 160.00,
        "product_type": "INTRADAY",
    }
    view = tenant_routes._normalize_position_view(pos)
    # (151.20 - 160.00) * 65 = -572.00
    assert view["unrealized_pnl"] == pytest.approx(-572.0)


def test_explicit_zero_avg_price_is_not_overridden_by_entry_price(caplog):
    """Codex P2 #3: the dict normalization used to be
    ``row.get('avg_price') or ... or row.get('entry_price')`` -- a row
    with avg_price=0 explicitly set AND a non-zero entry_price would
    fall through to entry_price (Python `or` skips falsy 0), hiding
    the corrupt avg_price from the defence below. The new
    `_first_present` helper preserves explicit zeros."""
    pos = {
        "symbol": "NIFTY12MAY2624250CE",
        "quantity": 65,
        "avg_price": 0.0,        # explicitly corrupt
        "entry_price": 160.00,   # plausible-looking fallback
        "product_type": "INTRADAY",
    }
    with caplog.at_level("WARNING"):
        view = tenant_routes._normalize_position_view(pos)

    # avg_price preserved at the corrupt value -- not overridden by
    # entry_price.
    assert view["avg_price"] == 0.0
    # Defence fires (would not have fired if entry_price had silently
    # taken over).
    assert view["unrealized_pnl"] is None
    assert any(
        "POSITION_VIEW_AVG_PRICE_INVALID" in rec.getMessage()
        for rec in caplog.records
    )


def test_account_snapshot_surfaces_invalid_marks_count():
    """Codex P2 #1: when _normalize_position_view returns
    unrealized_pnl=None for a non-zero open row, the account snapshot
    must NOT silently coalesce it to 0.0. Instead it must count the
    invalid mark in `invalid_marks_count` so the PnL endpoint can
    surface the desync to operators."""
    positions = [
        # Valid row: contributes to unrealized.
        {
            "symbol": "NIFTY1",
            "quantity": 50,
            "avg_price": 100.0,
            "product_type": "INTRADAY",
        },
        # Corrupt row: avg_price=0 with non-zero qty + LTP -> invalid mark.
        {
            "symbol": "NIFTY12MAY2624250CE",
            "quantity": 65,
            "avg_price": 0.0,
            "product_type": "INTRADAY",
        },
    ]
    snapshot = tenant_routes._build_live_account_mark_snapshot(positions)

    # The invalid row is counted, NOT zero-coalesced into the unrealized
    # total.
    assert snapshot["invalid_marks_count"] == 1
    # Valid row contributed: (151.20 - 100.0) * 50 = 2560.0
    assert snapshot["unrealized_pnl"] == pytest.approx(2560.0)


def test_account_snapshot_zero_invalid_marks_when_all_rows_clean():
    """Sanity counterpart: when all rows are clean, invalid_marks_count
    is 0."""
    positions = [
        {
            "symbol": "NIFTY1",
            "quantity": 50,
            "avg_price": 100.0,
            "product_type": "INTRADAY",
        }
    ]
    snapshot = tenant_routes._build_live_account_mark_snapshot(positions)
    assert snapshot["invalid_marks_count"] == 0
    assert snapshot["unrealized_pnl"] == pytest.approx(2560.0)


def test_no_ltp_returns_none_unrealized_without_alerting(caplog, monkeypatch):
    """When LTP is unknown and avg_price=0, the result is still None
    (can't compute) but the ALERT is suppressed (no LTP means no
    misleading-profit risk)."""
    monkeypatch.setattr(
        tenant_routes.dashboard_bus,
        "get_last_price_for_instrument",
        lambda *, symbol=None, token=None: None,
    )
    monkeypatch.setattr(
        tenant_routes.dashboard_bus,
        "get_last_price",
        lambda symbol: None,
    )
    pos = {
        "symbol": "NIFTY12MAY2624250CE",
        "quantity": 65,
        "avg_price": 0.0,
        "product_type": "INTRADAY",
    }
    with caplog.at_level("WARNING"):
        view = tenant_routes._normalize_position_view(pos)

    assert view["unrealized_pnl"] is None
    assert view["ltp"] is None
    assert not any(
        "POSITION_VIEW_AVG_PRICE_INVALID" in rec.getMessage()
        for rec in caplog.records
    ), "No LTP -> no misleading-profit risk -> no ALERT"
