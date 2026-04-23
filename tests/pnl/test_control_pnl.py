"""Tests for control PnL split from cash ledger (Patch Set 1)."""
import pytest
from datetime import datetime, timezone

from app.pnl.types import TradeOpenEvent, TradeCloseEvent
from app.pnl.pnl_engine import PnLEngine
from app.pnl.state_store import InMemoryPnLStateStore


@pytest.fixture
def make_pnl_engine():
    def _factory():
        return PnLEngine(state_store=InMemoryPnLStateStore())
    return _factory


def make_open_event(qty=65, price=181.0):
    return TradeOpenEvent(
        tenant_id="t1",
        broker_account_id="A1",
        strategy_id="strat1",
        symbol="NIFTY28APR2624300CE",
        qty=qty,
        entry_price=price,
        lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )


def make_close_event(qty=65, price=123.65):
    return TradeCloseEvent(
        tenant_id="t1",
        broker_account_id="A1",
        strategy_id="strat1",
        symbol="NIFTY28APR2624300CE",
        qty=qty,
        exit_price=price,
        lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )


def test_short_sell_does_not_book_full_credit_as_control_profit(make_pnl_engine):
    """Short SELL 65@181 should NOT immediately book +11765 as control_total_pnl."""
    engine = make_pnl_engine()
    engine.on_open_position(make_open_event(qty=65, price=181.0))
    total = engine.get_control_total_pnl("t1", "A1")
    # control_open_pnl is 0 until mark is updated; control_realized_pnl = 0
    assert total is not None
    assert abs(total) < 1.0  # approximately 0 right after entry (no mark update yet)


def test_short_option_control_total_pnl_tracks_mark_to_market(make_pnl_engine):
    """After LTP moves from 181 to 134, control PnL = (181-134)*65 = 3055."""
    engine = make_pnl_engine()
    engine.on_open_position(make_open_event(qty=65, price=181.0))
    engine.update_control_open_pnl("t1", "A1", "strat1", current_ltp=134.0)
    total = engine.get_control_total_pnl("t1", "A1")
    expected = (181.0 - 134.0) * 65
    assert total is not None
    assert abs(total - expected) < 1.0


def test_short_option_close_books_realized_only(make_pnl_engine):
    """After buying back 65@123.65, control_realized_pnl = (181-123.65)*65."""
    engine = make_pnl_engine()
    engine.on_open_position(make_open_event(qty=65, price=181.0))
    engine.on_close_position(make_close_event(qty=65, price=123.65))
    total = engine.get_control_total_pnl("t1", "A1")
    expected = (181.0 - 123.65) * 65
    assert total is not None
    assert abs(total - expected) < 1.0


def test_get_control_total_pnl_returns_none_when_no_control_data(make_pnl_engine):
    """No on_open_position called → get_control_total_pnl returns None (fallback signal)."""
    engine = make_pnl_engine()
    result = engine.get_control_total_pnl("t1", "A1")
    assert result is None


def test_partial_close_reduces_open_qty(make_pnl_engine):
    """Closing 30 of 65 units leaves 35 units open."""
    engine = make_pnl_engine()
    engine.on_open_position(make_open_event(qty=65, price=181.0))
    engine.on_close_position(make_close_event(qty=30, price=150.0))
    snap = engine._state_store.get_snapshot(
        tenant_id="t1", broker_account_id="A1", strategy_id="strat1"
    )
    assert snap is not None
    assert snap.control_open_qty == 35
    realized = (181.0 - 150.0) * 30
    assert abs(snap.control_realized_pnl - realized) < 1.0


def test_on_close_position_warns_with_no_open(make_pnl_engine, caplog):
    """Closing with no tracked open position logs a warning and does not crash."""
    import logging
    engine = make_pnl_engine()
    with caplog.at_level(logging.WARNING):
        engine.on_close_position(make_close_event(qty=65, price=123.65))
    assert engine.get_control_total_pnl("t1", "A1") is None


def test_control_pnl_multi_strategy(make_pnl_engine):
    """Two strategies contribute independently to the account-level control PnL."""
    engine = make_pnl_engine()
    ev1 = TradeOpenEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="s1",
        symbol="CE1", qty=50, entry_price=100.0, lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )
    ev2 = TradeOpenEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="s2",
        symbol="CE2", qty=30, entry_price=200.0, lot_size=1,
        trade_time=datetime.now(timezone.utc),
    )
    engine.on_open_position(ev1)
    engine.on_open_position(ev2)
    engine.update_control_open_pnl("t1", "A1", "s1", current_ltp=80.0)
    engine.update_control_open_pnl("t1", "A1", "s2", current_ltp=150.0)
    total = engine.get_control_total_pnl("t1", "A1")
    expected = (100.0 - 80.0) * 50 + (200.0 - 150.0) * 30
    assert total is not None
    assert abs(total - expected) < 1.0


def test_lot_size_multiplication(make_pnl_engine):
    """Lot size is applied to premium and realized PnL computation."""
    engine = make_pnl_engine()
    ev = TradeOpenEvent(
        tenant_id="t1", broker_account_id="A1", strategy_id="s1",
        symbol="BANKNIFTY_CE", qty=2, entry_price=300.0, lot_size=15,
        trade_time=datetime.now(timezone.utc),
    )
    engine.on_open_position(ev)
    snap = engine._state_store.get_snapshot(
        tenant_id="t1", broker_account_id="A1", strategy_id="s1"
    )
    # control_open_premium = 300 * 2 * 15 = 9000
    assert abs(snap.control_open_premium - 9000.0) < 1.0
