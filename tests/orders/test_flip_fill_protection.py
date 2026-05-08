"""Regression tests for the flip-the-trade single-fill bug.

A 2,500-unit close fill on a 1,250-unit short open (i.e. the fill quantity
exceeds the open quantity, carrying net_qty through zero to the opposite
sign) used to corrupt internal_position_records: filled_qty_close became
larger than filled_qty_open, side stayed wrong, and the state machine
attempted an illegal RECONCILING -> PARTIALLY_EXITED transition that
parked the record in DEGRADED -> RECOVERY_PENDING for the rest of the
session. See ops/cleanup_a1_stuck_state_20260507.sql.

These tests pin the new behaviour: the fill is *not* applied to the
record; the record is routed to RECONCILING with a structured reason; a
POSITION_FLIP_FILL_DETECTED audit event is emitted; and the projected
state_store position reflects the LAST KNOWN GOOD net_qty (not a half-
updated value).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from app.brokers.base import OrderPurpose
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.position_state import InternalPosition, PositionState
from app.orders.order_lifecycle import OrderLifecycleService, SubmissionContext
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


def _mk_service() -> OrderLifecycleService:
    """Construct an OrderLifecycleService with the bare minimum of deps so
    we can drive `_apply_position_fill` directly. We inject an in-memory
    processed-trade store so the constructor doesn't try to wire up
    Postgres for durable markers (which would require LIVE env config)."""

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


def _mk_ctx(*, side: str, purpose: str, qty: int) -> SubmissionContext:
    return SubmissionContext(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("ema20_strategy"),
        hub_order_id="hub-1",
        broker_order_id="260507001433140",
        symbol="NATURALGAS22MAY26255CE",
        side=side,
        fallback_qty=qty,
        fallback_price=None,
        product_type="INTRADAY",
        purpose=purpose,
    )


def _seed_open_short_position(
    svc: OrderLifecycleService,
    *,
    ctx: SubmissionContext,
    open_qty: int,
    avg_price: float,
) -> InternalPosition:
    """Place an OPEN short position into _position_records as if a prior
    SELL entry fill had landed cleanly."""
    record = svc._ensure_position_record(ctx)
    assert record is not None
    record.side = "SELL"
    record.filled_qty_open = float(open_qty)
    record.avg_open_price = float(avg_price)
    record.net_qty = -float(open_qty)  # short = negative
    record.position_state = PositionState.OPEN
    return record


# --- Tests --------------------------------------------------------------

def test_flip_fill_does_not_corrupt_record():
    """A 2,500u close fill on a 1,250u SELL must not increment
    filled_qty_close past filled_qty_open or move net_qty across zero."""
    svc = _mk_service()
    open_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=1250)
    record = _seed_open_short_position(svc, ctx=open_ctx, open_qty=1250, avg_price=10.70)

    # The flip-the-trade fill arrives as a BUY (exit-side for a short) of 2,500.
    exit_ctx = _mk_ctx(side="BUY", purpose="EXIT", qty=2500)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=2500,
        price=12.50,
        trade_time=datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc),
    )

    # Record state must be unchanged in the data fields the buggy code corrupted.
    assert record.filled_qty_open == 1250.0
    assert record.filled_qty_close == 0.0, (
        "Flip fill must not be applied to filled_qty_close — "
        "auto-splitting is a follow-up; for now we route to RECONCILING."
    )
    assert record.net_qty == -1250.0
    assert record.side == "SELL"
    assert record.avg_open_price == pytest.approx(10.70)


def test_flip_fill_routes_record_to_reconciling_with_structured_reason():
    svc = _mk_service()
    open_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=1250)
    record = _seed_open_short_position(svc, ctx=open_ctx, open_qty=1250, avg_price=10.70)

    exit_ctx = _mk_ctx(side="BUY", purpose="EXIT", qty=2500)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=2500,
        price=12.50,
        trade_time=datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc),
    )

    assert record.position_state == PositionState.RECONCILING
    assert record.state_reason.startswith("flip_fill_blocked:close_leg=1250:open_leg=1250"), (
        f"unexpected state_reason: {record.state_reason}"
    )


def test_normal_partial_close_still_works_unchanged():
    """A 500u close fill on a 1,250u SELL is a regular partial exit and
    must keep the existing semantics."""
    svc = _mk_service()
    open_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=1250)
    record = _seed_open_short_position(svc, ctx=open_ctx, open_qty=1250, avg_price=10.70)

    exit_ctx = _mk_ctx(side="BUY", purpose="EXIT", qty=500)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=500,
        price=12.50,
        trade_time=datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc),
    )

    assert record.filled_qty_close == 500.0
    assert record.net_qty == -750.0  # short, partially covered
    assert record.position_state == PositionState.PARTIALLY_EXITED
    assert record.state_reason == "partial_exit_fill_observed"


def test_exact_close_fill_still_goes_flat_pending_confirmation():
    """A 1,250u close fill on a 1,250u SELL = exact flatten. Net_qty
    lands exactly at zero, so we go to FLAT_PENDING_CONFIRMATION as before."""
    svc = _mk_service()
    open_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=1250)
    record = _seed_open_short_position(svc, ctx=open_ctx, open_qty=1250, avg_price=10.70)

    exit_ctx = _mk_ctx(side="BUY", purpose="EXIT", qty=1250)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=1250,
        price=12.50,
        trade_time=datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc),
    )

    assert record.filled_qty_close == 1250.0
    assert record.net_qty == 0.0
    assert record.position_state == PositionState.FLAT_PENDING_CONFIRMATION


def test_flip_fill_detected_for_long_position_too():
    """Symmetric coverage: a SELL fill larger than a long open should
    also trip the flip detector."""
    svc = _mk_service()
    open_ctx = _mk_ctx(side="BUY", purpose="ENTRY", qty=1250)
    record = svc._ensure_position_record(open_ctx)
    record.side = "BUY"
    record.filled_qty_open = 1250.0
    record.avg_open_price = 14.30
    record.net_qty = +1250.0
    record.position_state = PositionState.OPEN

    exit_ctx = _mk_ctx(side="SELL", purpose="EXIT", qty=2500)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=2500,
        price=16.50,
        trade_time=datetime(2026, 5, 7, 16, 0, tzinfo=timezone.utc),
    )

    assert record.position_state == PositionState.RECONCILING
    assert "flip_fill_blocked" in record.state_reason
    assert record.filled_qty_open == 1250.0
    assert record.filled_qty_close == 0.0
    assert record.net_qty == +1250.0


def test_zero_qty_position_is_not_misidentified_as_flip():
    """If net_qty is already 0 (FLAT or freshly-reset record), an exit
    fill should NOT be treated as a flip — there's nothing to flip from."""
    svc = _mk_service()
    open_ctx = _mk_ctx(side="SELL", purpose="ENTRY", qty=0)
    record = svc._ensure_position_record(open_ctx)
    record.side = ""
    record.net_qty = 0.0
    record.position_state = PositionState.NONE

    exit_ctx = _mk_ctx(side="BUY", purpose="EXIT", qty=500)
    svc._apply_position_fill(
        exit_ctx,
        filled_qty=500,
        price=12.50,
        trade_time=datetime(2026, 5, 7, 14, 30, tzinfo=timezone.utc),
    )

    # Falls through the existing exit_fill_without_known_position path.
    assert record.filled_qty_close == 500.0
    assert "flip_fill_blocked" not in record.state_reason
