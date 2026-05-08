"""Integration tests for PositionTrailingLockEngine.

Verifies that the periodic engine:
  - Skips when disabled.
  - Reads each runner's open positions, computes unrealized via the LTP cache,
    consults PositionTrailingLockManager, and submits a single-position exit
    order when the manager says exit_required.
  - Closes (resets) state for positions whose qty has gone to 0.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

from app.brokers.base import Position, ProductType
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, TenantId
from app.data.state_store import StateStore
from app.hub.exit_engines import PositionTrailingLockEngine
from app.pnl.position_trailing_lock import (
    PositionTrailingLockManager,
    _NoopPositionTrailingLockBackend,
)


def _mk_settings(**overrides) -> SimpleNamespace:
    base = dict(
        position_trailing_lock_enabled=True,
        position_trailing_lock_giveback_pct=0.10,
        position_trailing_lock_floor_inr=500.0,
        position_trailing_lock_exit_cooldown_seconds=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingOrderRouter:
    """Captures order submissions instead of dispatching to a broker."""

    def __init__(self) -> None:
        self.calls: List[dict] = []

    async def submit_order(
        self,
        *,
        tenant_id,
        broker_account_id,
        strategy_id,
        order_req,
    ):
        self.calls.append(
            {
                "tenant_id": str(tenant_id),
                "broker_account_id": str(broker_account_id),
                "strategy_id": str(strategy_id),
                "symbol": order_req.symbol,
                "side": order_req.side,
                "purpose": order_req.purpose,
                "exit_reason": order_req.exit_reason,
                "qty": order_req.quantity,
            }
        )
        return ("hub-order-id", SimpleNamespace(status="OK"))


def _runner(tenant_id: str, broker_account_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=TenantId(tenant_id),
        broker_account_id=BrokerAccountId(broker_account_id),
    )


def _seed_ltp(symbol: str, price: float, *, lot_size: int = 1250) -> None:
    """Inject an LTP and lot-size meta so the engine can compute unrealized
    AND build a valid exit plan (build_position_exit_plan rejects derivative
    symbols without a known lot size).
    """
    from datetime import datetime, timezone

    dashboard_bus.record_tick(symbol, price, datetime.now(timezone.utc))
    dashboard_bus.upsert_instrument_meta(symbol, {"symbol": symbol, "lot_size": lot_size})


@pytest.mark.asyncio
async def test_engine_disabled_does_nothing():
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    _seed_ltp("NG22MAY26255CE", 16.50)

    router = _RecordingOrderRouter()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(position_trailing_lock_enabled=False),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == []


@pytest.mark.asyncio
async def test_engine_does_not_exit_below_arming_floor():
    state_store = StateStore()
    # Tiny profit: (14.40 - 14.30) * 1250 = 125 < 500 floor.
    state_store.set_positions(
        "A1",
        [Position(symbol="NG_TINY", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    _seed_ltp("NG_TINY", 14.40)

    router = _RecordingOrderRouter()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == []


@pytest.mark.asyncio
async def test_engine_emits_exit_when_giveback_breached():
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RecordingOrderRouter()
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
    )

    # Cycle 1: peak climbs to 2,750.
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], "no exit while at peak"

    # Cycle 2: LTP pulls back to 16.27 -> unrealized = (16.27-14.30)*1250 = 2,462.5
    # which is below 2,750 * 0.9 = 2,475 — should exit.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["symbol"] == "NG22MAY26255CE"
    assert call["exit_reason"] == "position_giveback_breach"
    assert call["broker_account_id"] == "A1"
    # Exit side must be opposite of position (BUY 1250 -> SELL).
    from app.brokers.base import OrderSide

    assert call["side"] == OrderSide.SELL


@pytest.mark.asyncio
async def test_engine_resets_state_when_position_qty_drops_to_zero():
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG_X", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    _seed_ltp("NG_X", 16.50)

    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=_RecordingOrderRouter(),  # type: ignore[arg-type]
        manager=manager,
    )
    # Build a peak.
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # Verify state has a peak now.
    key = ("t-1", "A1", "NG_X")
    assert key in manager._states
    assert manager._states[key].peak_unrealized_pnl > 0

    # Position closes (qty=0).
    state_store.set_positions(
        "A1",
        [Position(symbol="NG_X", quantity=0, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key not in manager._states


@pytest.mark.asyncio
async def test_engine_skips_position_with_no_ltp():
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NO_LTP_SYMBOL_XYZ", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    # NB: we deliberately do NOT seed an LTP for this symbol.
    router = _RecordingOrderRouter()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # No exits, no errors.
    assert router.calls == []


# ---------------------------------------------------------------------------
# Issue #219 — trailing-lock must NOT submit exits while the durable kill
# switch is tripped for the runner's scope. The 2026-05-08 NATURALGAS
# incident showed that, while BROKER_SYNC is suppressed during a kill-switch
# active window, internal position state goes stale and trailing-lock has
# historically fired runaway exits against fiction.
# ---------------------------------------------------------------------------


class _StubKillSwitchManager:
    """Minimal stand-in for KillSwitchManager.is_tripped_for_scope()."""

    def __init__(self, *, tripped: bool = False) -> None:
        self.tripped = tripped
        self.calls: List[dict] = []

    def is_tripped_for_scope(
        self,
        *,
        tenant_id=None,
        account_id=None,
        strategy_id=None,
    ) -> bool:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "account_id": account_id,
                "strategy_id": strategy_id,
            }
        )
        return self.tripped


@pytest.mark.asyncio
async def test_engine_skips_exit_when_kill_switch_tripped_for_scope():
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    router = _RecordingOrderRouter()
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    ksm = _StubKillSwitchManager(tripped=True)
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
        kill_switch_manager_provider=lambda ksm=ksm: ksm,
    )

    # Cycle 1: peak climbs above the floor.
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    # Cycle 2: pullback past giveback — would normally submit an exit.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert router.calls == [], (
        "trailing-lock must not route exits while kill switch is tripped (#219)"
    )
    # The engine did query the kill switch on each evaluate cycle.
    assert len(ksm.calls) >= 2
    assert ksm.calls[0]["account_id"] == "A1"


@pytest.mark.asyncio
async def test_engine_emits_exit_when_kill_switch_inactive():
    """When kill_switch_manager is supplied but not tripped, engine behaves
    exactly as without one — proves the gate is non-disruptive in production."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    router = _RecordingOrderRouter()
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    ksm = _StubKillSwitchManager(tripped=False)
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
        kill_switch_manager_provider=lambda ksm=ksm: ksm,
    )

    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert len(router.calls) == 1, (
        "kill_switch_manager presence must not change behaviour when not tripped"
    )
    assert router.calls[0]["exit_reason"] == "position_giveback_breach"


@pytest.mark.asyncio
async def test_engine_logs_skip_event_with_rate_limit():
    """The skip log is emitted at most once per (account) per 60s, so a long
    kill-switch window does not flood logs."""
    import logging
    from app.hub import exit_engines as ee_mod

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    router = _RecordingOrderRouter()
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    ksm = _StubKillSwitchManager(tripped=True)
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
        kill_switch_manager_provider=lambda ksm=ksm: ksm,
    )

    captured: List[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _CaptureHandler(level=logging.WARNING)
    ee_mod.logger.addHandler(handler)
    try:
        _seed_ltp("NG22MAY26255CE", 16.50)
        for _ in range(5):
            await engine.evaluate_runners([_runner("t-1", "A1")])
    finally:
        ee_mod.logger.removeHandler(handler)

    skip_events = [
        m for m in captured if "POSITION_TRAILING_LOCK_SKIPPED_KILL_SWITCH" in m
    ]
    assert len(skip_events) == 1, (
        f"expected exactly 1 rate-limited skip log within 60s window, got {len(skip_events)}: {skip_events}"
    )


@pytest.mark.asyncio
async def test_engine_observes_kill_switch_manager_swap_via_provider():
    """Codex review on PR #231: AppRuntime replaces
    HubRuntime.kill_switch_manager AFTER engine construction. The engine
    must observe the post-load instance, not the original empty pre-load
    one. Proven here by holding the KSM in a mutable container the
    provider closure reads at evaluate time."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    router = _RecordingOrderRouter()
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())

    # Container that simulates AppRuntime.kill_switch_manager attribute
    # being replaced after the engine is constructed.
    container: dict = {"ksm": _StubKillSwitchManager(tripped=False)}

    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
        kill_switch_manager_provider=lambda: container["ksm"],
    )

    # Cycle 1: build a peak with the original (untripped) KSM.
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], "no exit while at peak"

    # AppRuntime replaces the KSM with a tripped instance loaded from
    # Postgres. The provider closure must observe the swap.
    container["ksm"] = _StubKillSwitchManager(tripped=True)

    # Cycle 2: pullback past giveback — would normally route an exit.
    # With a stale (pre-swap) reference, the engine would still see an
    # untripped manager and submit. With a provider closure, it sees the
    # swap and skips.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert router.calls == [], (
        "engine must observe the post-swap KillSwitchManager via provider "
        "closure (PR #231 review)"
    )
