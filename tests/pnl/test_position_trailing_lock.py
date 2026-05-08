"""Unit tests for PositionTrailingLockManager."""

from __future__ import annotations

import pytest

from app.core.identifiers import BrokerAccountId, TenantId
from app.pnl.position_trailing_lock import (
    PositionTrailingLockManager,
    _NoopPositionTrailingLockBackend,
)


def _mk_manager() -> PositionTrailingLockManager:
    return PositionTrailingLockManager(
        default_time_zone="Asia/Kolkata",
        backend=_NoopPositionTrailingLockBackend(),
    )


def test_first_evaluation_seeds_peak_and_does_not_exit():
    mgr = _mk_manager()
    decision = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NATURALGAS22MAY26255CE",
        current_unrealized_pnl=200.0,  # below floor
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=30.0,
    )
    assert decision.peak_unrealized_pnl == 200.0
    assert decision.armed is False
    assert decision.exit_required is False


def test_does_not_arm_until_peak_crosses_floor():
    mgr = _mk_manager()
    # Peak walks up but stays below floor — never arms.
    for v in [100.0, 200.0, 400.0, 499.0]:
        d = mgr.evaluate(
            tenant_id=TenantId("t-1"),
            broker_account_id=BrokerAccountId("A1"),
            symbol="X",
            current_unrealized_pnl=v,
            floor_inr=500.0,
            giveback_pct=0.10,
            exit_cooldown_seconds=30.0,
        )
        assert d.armed is False
        assert d.exit_required is False
    # Crosses floor — arms but does not yet exit.
    d = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="X",
        current_unrealized_pnl=600.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=30.0,
    )
    assert d.armed is True
    assert d.exit_required is False
    assert d.peak_unrealized_pnl == 600.0


def test_exit_fires_when_current_drops_giveback_pct_below_peak():
    mgr = _mk_manager()
    # Walk peak up.
    for v in [600.0, 1500.0, 2750.0]:
        mgr.evaluate(
            tenant_id=TenantId("t-1"),
            broker_account_id=BrokerAccountId("A1"),
            symbol="NG",
            current_unrealized_pnl=v,
            floor_inr=500.0,
            giveback_pct=0.10,
            exit_cooldown_seconds=0.0,
        )
    # Drop to 90% of peak (2475.0) — exactly the floor; not yet below.
    d = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=2475.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert d.exit_required is False  # strict <
    # One paisa below — fires.
    d = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=2474.99,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert d.exit_required is True
    assert d.exit_reason == "position_giveback_breach"
    assert d.peak_unrealized_pnl == pytest.approx(2750.0)


def test_peak_only_ratchets_up_never_down():
    mgr = _mk_manager()
    mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=1000.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    # Drop, then partial recovery — peak should stay 1000.
    d = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=950.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert d.peak_unrealized_pnl == 1000.0


def test_cooldown_suppresses_repeat_exits():
    mgr = _mk_manager()
    mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=1000.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    # Trigger first exit.
    d1 = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=800.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=60.0,
    )
    assert d1.exit_required is True
    # Immediate re-evaluation within cooldown — should suppress.
    d2 = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=750.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=60.0,
    )
    assert d2.exit_required is False
    assert d2.exit_reason == "exit_cooldown"


def test_reset_position_clears_state_so_new_position_starts_fresh():
    mgr = _mk_manager()
    mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=2000.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    mgr.reset_position(TenantId("t-1"), BrokerAccountId("A1"), "NG")
    # New position re-opens — should not inherit old peak.
    d = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG",
        current_unrealized_pnl=100.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert d.peak_unrealized_pnl == 100.0
    assert d.armed is False


def test_independent_state_per_symbol():
    mgr = _mk_manager()
    # Symbol A: armed and breached.
    mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="A",
        current_unrealized_pnl=1000.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    da = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="A",
        current_unrealized_pnl=800.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert da.exit_required is True
    # Symbol B: untouched, low peak.
    db = mgr.evaluate(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="B",
        current_unrealized_pnl=300.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert db.exit_required is False
    assert db.armed is False


def test_user_scenario_naturalgas_2750_to_2475():
    """Reproduces the exact user-stated requirement on real numbers.

    Position: NATURALGAS BUY 1250 @ 14.30. Peak unrealized hits 2,750
    (LTP=16.50). When LTP falls back so unrealized = 2,474 (LTP=16.279),
    the engine should signal exit at 90% peak threshold.
    """
    mgr = _mk_manager()
    args = dict(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NATURALGAS22MAY26255CE",
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    # Climb.
    for v in [500.0, 1000.0, 1875.0, 2312.5, 2750.0]:
        d = mgr.evaluate(current_unrealized_pnl=v, **args)
    assert d.peak_unrealized_pnl == 2750.0
    assert d.armed is True
    # Pullback to 2,475 — exactly threshold; no exit (strict <).
    d = mgr.evaluate(current_unrealized_pnl=2475.0, **args)
    assert d.exit_required is False
    # Pullback to 2,474 — exit fires.
    d = mgr.evaluate(current_unrealized_pnl=2474.0, **args)
    assert d.exit_required is True
    assert d.exit_reason == "position_giveback_breach"
