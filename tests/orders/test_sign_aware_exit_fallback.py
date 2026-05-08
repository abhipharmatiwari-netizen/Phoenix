"""Issue #204 Layer 2: sign-aware exit fallback in _apply_position_fill.

The 2026-05-08 NIFTY24250CE incident showed Phoenix booking a phantom
OPEN long with avg_price=0 after a SHORT was covered by a fill whose
SubmissionContext lacked purpose=EXIT. Without the fallback, the BUY
fill reached the entry path and added to filled_qty_open instead of
closing the SHORT.

These tests pin the new behaviour: when sign(signed_qty) opposes
sign(record.net_qty), the fill is reclassified as exit-like regardless
of ctx.purpose, a structured POSITION_FILL_EXIT_INFERRED_FROM_SIGN
WARNING is emitted, and the existing exit branch (including flip-fill
detection from PR #199) runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.position_state import InternalPosition, PositionState
from app.orders.order_lifecycle import OrderLifecycleService, SubmissionContext
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


def _mk_service() -> OrderLifecycleService:
    class _NoopStateStore:
        def get_balance(self, *_a, **_kw): return None
        def get_positions(self, *_a, **_kw): return []
        def set_positions(self, *_a, **_kw): return None
        def set_position(self, *_a, **_kw): return None
        def upsert_position(self, *_a, **_kw): return None

    return OrderLifecycleService(
        state_store=_NoopStateStore(),
        pnl_engine=None,
        position_ownership_store=None,
        processed_store=InMemoryProcessedTradeStore(),
    )


def _mk_ctx(
    *, side: str, purpose: str, qty: int, broker_order_id: str = "260508000344339"
) -> SubmissionContext:
    return SubmissionContext(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("ema20_strategy"),
        hub_order_id="hub-1",
        broker_order_id=broker_order_id,
        symbol="NIFTY12MAY2624250CE",
        side=side,
        fallback_qty=qty,
        fallback_price=None,
        product_type="INTRADAY",
        purpose=purpose,
    )


def _seed_open_short(svc: OrderLifecycleService, *, qty: int, avg_price: float) -> InternalPosition:
    open_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=qty, broker_order_id="260508000312035")
    record = svc._ensure_position_record(open_ctx)
    assert record is not None
    record.side = "SELL"
    record.filled_qty_open = float(qty)
    record.avg_open_price = float(avg_price)
    record.net_qty = -float(qty)
    record.position_state = PositionState.OPEN
    return record


def _seed_open_long(svc: OrderLifecycleService, *, qty: int, avg_price: float) -> InternalPosition:
    open_ctx = _mk_ctx(side="BUY", purpose="ENTRY", qty=qty, broker_order_id="260508000312030")
    record = svc._ensure_position_record(open_ctx)
    assert record is not None
    record.side = "BUY"
    record.filled_qty_open = float(qty)
    record.avg_open_price = float(avg_price)
    record.net_qty = +float(qty)
    record.position_state = PositionState.OPEN
    return record


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_buy_against_short_with_purpose_entry_is_reclassified_as_exit(caplog):
    """The 2026-05-08 NIFTY24250CE incident: a BUY fill arrived against a
    1-lot (65) SHORT @160.00 with ctx.purpose != EXIT. Without the
    fallback, this lands in the entry path and corrupts the record.
    With the fallback, it is reclassified as exit and the SHORT is
    correctly covered."""
    svc = _mk_service()
    record = _seed_open_short(svc, qty=65, avg_price=160.00)

    cover_ctx = _mk_ctx(side="BUY", purpose="ENTRY", qty=65)
    with caplog.at_level("WARNING"):
        svc._apply_position_fill(
            cover_ctx,
            filled_qty=65,
            price=150.20,
            trade_time=datetime(2026, 5, 8, 10, 58, 23, tzinfo=timezone.utc),
        )

    # Reclassified as exit -> SHORT closed, record went FLAT_PENDING_CONFIRMATION.
    assert record.filled_qty_close == 65.0
    assert record.filled_qty_open == 65.0
    assert record.net_qty == 0.0
    assert record.position_state == PositionState.FLAT_PENDING_CONFIRMATION
    assert record.avg_close_price == pytest.approx(150.20)
    # avg_open_price preserved -- not corrupted by being treated as fresh entry.
    assert record.avg_open_price == pytest.approx(160.00)
    # Structured WARNING was emitted for observability.
    assert any(
        "position_fill_exit_inferred_from_sign" in rec.getMessage().lower()
        or "POSITION_FILL_EXIT_INFERRED_FROM_SIGN" in rec.getMessage()
        for rec in caplog.records
    )


def test_sell_against_long_with_purpose_entry_is_reclassified_as_exit():
    """Symmetric coverage: a SELL fill against a LONG record with
    purpose=ENTRY must also be reclassified as exit."""
    svc = _mk_service()
    record = _seed_open_long(svc, qty=50, avg_price=14.30)

    cover_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=50)
    svc._apply_position_fill(
        cover_ctx,
        filled_qty=50,
        price=16.50,
        trade_time=datetime(2026, 5, 8, 11, 0, tzinfo=timezone.utc),
    )

    assert record.filled_qty_close == 50.0
    assert record.net_qty == 0.0
    assert record.position_state == PositionState.FLAT_PENDING_CONFIRMATION
    assert record.avg_close_price == pytest.approx(16.50)
    assert record.avg_open_price == pytest.approx(14.30)


def test_same_direction_fill_is_not_reclassified_as_exit():
    """Sanity: a BUY against a LONG (entry-add / scale-in) must NOT be
    reclassified. The fallback only fires when sign opposes."""
    svc = _mk_service()
    record = _seed_open_long(svc, qty=50, avg_price=14.30)

    add_ctx = _mk_ctx(side="BUY", purpose="ENTRY", qty=25)
    svc._apply_position_fill(
        add_ctx,
        filled_qty=25,
        price=15.00,
        trade_time=datetime(2026, 5, 8, 11, 5, tzinfo=timezone.utc),
    )

    # Entry path: filled_qty_open grew, net_qty grew, no close.
    assert record.filled_qty_open == 75.0
    assert record.filled_qty_close == 0.0
    assert record.net_qty == +75.0
    assert record.position_state == PositionState.OPEN
    # Weighted avg of 14.30 (50) + 15.00 (25) = (50*14.30 + 25*15.00) / 75 = 14.5333
    assert record.avg_open_price == pytest.approx((50 * 14.30 + 25 * 15.00) / 75, rel=1e-3)


def test_explicit_exit_purpose_still_takes_exit_path():
    """When ctx.purpose=EXIT is set correctly, behaviour is unchanged
    (no fallback log emitted, exit branch runs directly)."""
    svc = _mk_service()
    record = _seed_open_short(svc, qty=65, avg_price=160.00)

    exit_ctx = _mk_ctx(side="BUY", purpose="EXIT", qty=65)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=65,
        price=150.20,
        trade_time=datetime(2026, 5, 8, 10, 58, 23, tzinfo=timezone.utc),
    )

    assert record.filled_qty_close == 65.0
    assert record.net_qty == 0.0
    assert record.position_state == PositionState.FLAT_PENDING_CONFIRMATION


def test_fill_against_zero_position_is_not_reclassified():
    """When prior_net_qty == 0 (no existing position), the fallback must
    NOT fire -- a fresh BUY entry is just a fresh BUY entry."""
    svc = _mk_service()
    open_ctx = _mk_ctx(side="BUY", purpose="ENTRY", qty=50)
    record = svc._ensure_position_record(open_ctx)
    assert record is not None
    # Fresh record: net_qty == 0
    assert record.net_qty == 0.0

    svc._apply_position_fill(
        open_ctx,
        filled_qty=50,
        price=14.30,
        trade_time=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
    )

    # Entry path: this is a brand-new long, not a covered short.
    assert record.filled_qty_open == 50.0
    assert record.filled_qty_close == 0.0
    assert record.net_qty == +50.0
    assert record.position_state == PositionState.OPEN
    assert record.avg_open_price == pytest.approx(14.30)


def test_flip_fill_detection_still_runs_via_inferred_exit_path():
    """When the fallback reclassifies the fill as exit AND the fill is a
    flip-the-trade (qty exceeds open), the existing flip-fill RECONCILING
    route still fires (no regression on issue #197)."""
    svc = _mk_service()
    record = _seed_open_short(svc, qty=1250, avg_price=10.70)

    # 2500-unit BUY flip fill arrives with purpose=ENTRY (not EXIT).
    flip_ctx = _mk_ctx(side="BUY", purpose="ENTRY", qty=2500)
    svc._apply_position_fill(
        flip_ctx,
        filled_qty=2500,
        price=12.50,
        trade_time=datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc),
    )

    # Flip-fill RECONCILING route fired through the inferred-exit path:
    # record is in RECONCILING with a structured flip_fill_blocked reason
    # and the fill was NOT applied to filled_qty_close.
    assert record.position_state == PositionState.RECONCILING
    assert record.state_reason.startswith("flip_fill_blocked")
    assert record.filled_qty_close == 0.0
    assert record.net_qty == -1250.0
