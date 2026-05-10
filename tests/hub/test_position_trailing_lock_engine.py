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


class _RaisingKillSwitchManager:
    """Stub whose ``is_tripped_for_scope`` raises — simulates a transient
    Postgres outage or KSM lookup failure for the LIVE fail-closed test."""

    def is_tripped_for_scope(self, *, tenant_id=None, account_id=None, strategy_id=None):
        raise RuntimeError("simulated KSM lookup failure")


@pytest.mark.asyncio
async def test_engine_fails_closed_in_live_when_kill_switch_lookup_raises(monkeypatch):
    """Codex P2 round 2 review: in LIVE mode, a KSM lookup failure means we
    cannot prove the kill switch is INACTIVE. Trailing-lock MUST fail
    CLOSED (skip exit submission) — the GlobalKillSwitchInterceptor is
    NOT a backstop because it explicitly bypasses exit orders."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
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
    _seed_ltp("NG22MAY26255CE", 16.50)

    router = _RecordingOrderRouter()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        kill_switch_manager_provider=lambda: _RaisingKillSwitchManager(),
    )

    # Cycle 1: peak builds (no exit yet, just observation).
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # Cycle 2: pullback past giveback — would normally route an exit.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert router.calls == [], (
        "trailing-lock must fail CLOSED in LIVE when KSM lookup raises "
        "(Codex round-2 P2)"
    )


@pytest.mark.asyncio
async def test_engine_fails_open_in_paper_when_kill_switch_lookup_raises(monkeypatch):
    """In non-LIVE modes, preserve the historical fail-OPEN behaviour so
    KSM infrastructure flakiness does not block dev/paper loops. Risk is
    bounded — no real broker order is placed."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
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
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        kill_switch_manager_provider=lambda: _RaisingKillSwitchManager(),
    )

    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert len(router.calls) == 1, (
        "trailing-lock must fail OPEN in non-LIVE when KSM lookup raises "
        "(preserves dev-loop behaviour)"
    )


# ---------------------------------------------------------------------------
# Issue #225: per-position inflight idempotency. After a trailing-lock exit
# is submitted, subsequent evaluate cycles for the same (tenant, account,
# symbol) MUST block until the marker auto-clears (max age or position-
# closed). The 2026-05-08 incident produced duplicate 3-lot fills 842740 +
# 842946 ~60s apart because time-based cooldown alone was insufficient.
# ---------------------------------------------------------------------------


class _RouterReturningBrokerOrderId:
    """Router that returns a realistic ``(hub_order_id, OrderResponse)``
    tuple where the response carries a ``broker_order_id`` — required for
    the marker-arming path."""

    def __init__(self, broker_order_id: str = "BROKER-001") -> None:
        self.calls: List[dict] = []
        self._broker_order_id = broker_order_id

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append(
            {
                "tenant_id": str(tenant_id),
                "broker_account_id": str(broker_account_id),
                "strategy_id": str(strategy_id),
                "symbol": order_req.symbol,
                "side": order_req.side,
                "qty": order_req.quantity,
            }
        )
        return (
            "hub-order-id",
            SimpleNamespace(
                status="OK",
                broker_order_id=self._broker_order_id,
            ),
        )


@pytest.mark.asyncio
async def test_engine_blocks_duplicate_submission_via_inflight_marker():
    """Issue #225: after a trailing-lock exit is submitted, the next
    evaluate cycle for the SAME (tenant, account, symbol) MUST NOT
    submit another exit while the inflight marker is fresh."""
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
    router = _RouterReturningBrokerOrderId(broker_order_id="260508000842740")
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=60.0,
        ),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
    )

    # Cycle 1: peak builds.
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # Cycle 2: pullback past giveback → exit submitted.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert len(router.calls) == 1, "first cycle should submit"

    # Cycle 3: still under the giveback threshold — would normally fire
    # again. With marker set, MUST be blocked.
    _seed_ltp("NG22MAY26255CE", 16.20)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert len(router.calls) == 1, (
        "duplicate submission must be blocked by inflight marker (#225)"
    )

    # Marker contents are inspectable for diagnostics.
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers
    assert engine._inflight_markers[key][0] == "260508000842740"


@pytest.mark.asyncio
async def test_engine_clears_inflight_marker_when_position_closes():
    """When the position drops to qty=0, the broker order must have
    completed (terminal state); clear the marker so a re-opened position
    on the same symbol is not blocked by stale state."""
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
    router = _RouterReturningBrokerOrderId()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )

    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers, "marker armed after submit"

    # Position closes (qty=0).
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NG22MAY26255CE", quantity=0, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            )
        ],
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key not in engine._inflight_markers, (
        "qty=0 cleanup must clear the inflight marker (#225)"
    )


@pytest.mark.asyncio
async def test_engine_auto_clears_stale_inflight_marker_with_error_event():
    """If the marker is older than ``inflight_max_seconds``, the engine
    auto-clears it and emits POSITION_TRAILING_LOCK_INFLIGHT_TIMEOUT
    ERROR. The next eligible evaluate may submit a new exit."""
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
    router = _RouterReturningBrokerOrderId(broker_order_id="STALE-001")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=0.5,  # tiny for test
        ),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )

    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert len(router.calls) == 1

    # Wait for marker to age past the configured ceiling.
    import time as _t
    _t.sleep(0.7)

    captured: List[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _CaptureHandler(level=logging.ERROR)
    ee_mod.logger.addHandler(handler)
    try:
        # Cycle 3: marker now stale, must auto-clear with ERROR + allow
        # next submit.
        _seed_ltp("NG22MAY26255CE", 16.10)
        await engine.evaluate_runners([_runner("t-1", "A1")])
    finally:
        ee_mod.logger.removeHandler(handler)

    timeout_events = [
        m for m in captured if "POSITION_TRAILING_LOCK_INFLIGHT_TIMEOUT" in m
    ]
    assert len(timeout_events) >= 1, (
        "stale marker must emit ERROR INFLIGHT_TIMEOUT event (#225)"
    )
    assert len(router.calls) == 2, (
        "after stale-marker clear, next eligible cycle may submit"
    )


@pytest.mark.asyncio
async def test_inflight_marker_is_isolated_per_symbol():
    """A marker on symbol A must NOT block a submission for symbol B."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            Position(
                symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            ),
            Position(
                symbol="NG22MAY26260CE", quantity=1250, avg_price=14.30,
                product_type=ProductType.INTRADAY,
            ),
        ],
    )
    router = _RouterReturningBrokerOrderId()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )

    # Pre-arm marker for symbol A only.
    engine._set_inflight_marker(
        tenant_id=TenantId("t-1"),
        broker_account_id=BrokerAccountId("A1"),
        symbol="NG22MAY26255CE",
        broker_order_id="OTHER",
    )
    # Both symbols at peak then giveback.
    _seed_ltp("NG22MAY26255CE", 16.50)
    _seed_ltp("NG22MAY26260CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    _seed_ltp("NG22MAY26260CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    # Symbol B may submit; symbol A must be blocked.
    submitted_symbols = [c["symbol"] for c in router.calls]
    assert "NG22MAY26255CE" not in submitted_symbols, (
        "symbol-A marker must block A submission"
    )
    assert "NG22MAY26260CE" in submitted_symbols, (
        "symbol-A marker must NOT block symbol-B submission"
    )

