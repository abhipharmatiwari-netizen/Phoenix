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
