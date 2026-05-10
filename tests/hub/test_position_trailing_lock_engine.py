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


def _mark_positions_sync_fresh(
    state_store: StateStore, account_id: str,
) -> None:
    """Mark the broker positions sync as freshly succeeded for an
    account so the trailing-lock engine's disappeared-symbol sweep
    (which gates on sync freshness in PR #236 round-4 review P2)
    treats subsequent state-store snapshots as authoritative."""
    from datetime import datetime, timezone

    state_store.update_positions_status(
        account_id,
        status="OK",
        last_ok_ts=datetime.now(timezone.utc).isoformat(),
        last_count=0,
        error_reason=None,
        blocked_ts=None,
        retry_after_seconds=None,
    )


def _age_inflight_markers_past_grace(engine: PositionTrailingLockEngine) -> None:
    """Rewind every inflight marker timestamp so it appears older than
    the 5-second grace period of ``_sweep_disappeared_symbol_markers``.

    PR #236 round-6 review P3: replaces real ``time.sleep(5.5)`` calls
    that previously added 30+ seconds to this test module on every CI
    run. Disappeared-symbol coverage stays deterministic and fast.
    """
    import time as _t
    rewound = _t.monotonic() - 10.0  # well past the 5s grace
    for key, (broker_order_id, _ts) in list(engine._inflight_markers.items()):
        engine._inflight_markers[key] = (broker_order_id, rewound)


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



# ---------------------------------------------------------------------------
# Codex round-1 review on PR #236: 4 follow-up bugs.
# ---------------------------------------------------------------------------


class _RouterRejectingResponse:
    """Router that returns a REJECTED response (e.g. policy interceptor
    rejection or no active runner) — no broker order placed."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol, "side": order_req.side})
        return (
            "hub-order-id",
            SimpleNamespace(status="REJECTED", broker_order_id=""),
        )


class _RouterRaisingPostSubmit:
    """Router that places the broker order then RAISES (e.g. lifecycle
    persistence failed AFTER broker submitted). Simulates the post-
    broker-submit failure mode (PR #236 P1)."""

    def __init__(self, broker_order_id: str = "BROKER-RAISED"):
        self.calls: List[dict] = []
        self._boi = broker_order_id

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        raise RuntimeError(
            f"lifecycle_persist_failed_after_broker_submit boi={self._boi}"
        )


@pytest.mark.asyncio
async def test_marker_cleared_when_router_rejects_submission():
    """Codex P2 round 1: only KEEP the marker for accepted broker
    submissions. A REJECTED router response means no broker order was
    placed; the marker must be cleared so the next eligible cycle is
    not blocked by a non-existent in-flight order."""
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
    router = _RouterRejectingResponse()
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

    assert len(router.calls) == 1
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key not in engine._inflight_markers, (
        "REJECTED router response must clear the inflight marker (#236 P2)"
    )


@pytest.mark.asyncio
async def test_marker_remains_when_router_raises_post_submit():
    """Codex P1 round 1: when submit_order raises AFTER potentially
    placing the broker order, the marker MUST remain armed to preserve
    idempotency."""
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
    router = _RouterRaisingPostSubmit()
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
    assert key in engine._inflight_markers, (
        "post-submit raise must NOT clear the marker (#236 P1)"
    )


@pytest.mark.asyncio
async def test_marker_cleared_when_symbol_disappears_from_snapshot():
    """Codex P2 round 1: state stores can omit closed positions from
    the snapshot entirely. When a fill removes the symbol from the
    next snapshot, the marker must be swept (after grace period).

    PR #236 round-4 review P1: clearing now requires TWO consecutive
    missing snapshots — a single OK-but-empty broker poll is
    ambiguous and must not flush the duplicate-fill guard. This test
    therefore advances the empty-snapshot evaluation twice."""
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
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-1")
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
    assert key in engine._inflight_markers

    _age_inflight_markers_past_grace(engine)

    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    # First missing-symbol observation — round-4 P1 requires a
    # second consecutive miss before the marker is swept.
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "single missing snapshot is ambiguous — marker must persist "
        "until a second consecutive miss (#236 round-4 P1)"
    )
    # Second consecutive missing-symbol observation — now sweep.
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key not in engine._inflight_markers, (
        "marker must be swept after two consecutive missing snapshots "
        "(#236 round-4 P1)"
    )


@pytest.mark.asyncio
async def test_inflight_max_seconds_honours_settings_override():
    """Codex P1 round 1: position_trailing_lock_inflight_max_seconds
    must be readable from Settings — operators must be able to raise
    it above the watchdog cadence to prevent premature timeouts."""
    state_store = StateStore()
    router = _RouterReturningBrokerOrderId()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=300.0,
        ),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )
    assert engine._inflight_max_seconds() == 300.0


# ---------------------------------------------------------------------------
# Codex round-2 review on PR #236: 3 follow-up bugs.
# ---------------------------------------------------------------------------


class _RouterCancelledResponse:
    """Router that returns CANCELLED — also a terminal non-fill outcome."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return (
            "hub-order-id",
            SimpleNamespace(status="CANCELLED", broker_order_id=""),
        )


class _RouterExpiredResponse:
    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return (
            "hub-order-id",
            SimpleNamespace(status="EXPIRED", broker_order_id=""),
        )


@pytest.mark.asyncio
async def test_marker_cleared_for_cancelled_status():
    """Codex P2 round 2: CANCELLED is a terminal non-fill outcome —
    the marker must be cleared so the next eligible cycle is not
    blocked."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterCancelledResponse()
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
    assert key not in engine._inflight_markers, (
        "CANCELLED router response must clear the inflight marker (#236 round-2 P2)"
    )


@pytest.mark.asyncio
async def test_marker_cleared_for_expired_status():
    """Codex P2 round 2: EXPIRED is also a terminal non-fill outcome."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterExpiredResponse()
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
    assert key not in engine._inflight_markers, (
        "EXPIRED router response must clear the inflight marker (#236 round-2 P2)"
    )


@pytest.mark.asyncio
async def test_disappeared_symbol_sweep_also_resets_manager_state():
    """Codex P1 round 2: when a marker is swept because the symbol
    disappeared from the snapshot, the persisted
    PositionTrailingLockManager state must ALSO be reset. Without this,
    a quick same-symbol re-open evaluates against the prior peak/armed
    state and can immediately emit another trailing-lock exit."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    router = _RouterReturningBrokerOrderId()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
    )

    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    state_key = ("t-1", "A1", "NG22MAY26255CE")
    assert state_key in manager._states, "manager state should be present pre-sweep"

    _age_inflight_markers_past_grace(engine)

    # Symbol disappears from snapshot — PR #236 round-4 P1 requires
    # TWO consecutive missing snapshots before sweep / state reset.
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])  # 1st miss
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])  # 2nd miss

    assert state_key not in manager._states, (
        "manager peak/armed state must be reset when symbol disappears "
        "across two consecutive snapshots (#236 round-2 P1, round-4 P1)"
    )


# ---------------------------------------------------------------------------
# Codex round-4 review on PR #236.
# ---------------------------------------------------------------------------


class _RouterFullResponse:
    """Router returning FULL — Angel/other broker terminal-fill alias."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return (
            "hub-order-id",
            SimpleNamespace(status="FULL", broker_order_id="BOI-FULL"),
        )


class _RouterExecutedResponse:
    """Router returning EXECUTED — alternate terminal-fill alias."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return (
            "hub-order-id",
            SimpleNamespace(status="EXECUTED", broker_order_id="BOI-EX"),
        )


@pytest.mark.asyncio
async def test_stale_positions_sync_does_not_clear_marker():
    """Round-4 P2: when the broker positions sync is stale (no
    successful poll since the marker was armed), the disappeared-
    symbol sweep must NOT count the same stale empty snapshot as
    repeated missing observations. Otherwise a sync outage that
    leaves an empty snapshot in StateStore would cause the marker
    to be cleared after two evaluation cycles even though no fresh
    broker confirmation has arrived."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-1")
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
    assert key in engine._inflight_markers
    _age_inflight_markers_past_grace(engine)
    # Positions snapshot is empty AND sync is stale (no last_ok_ts
    # update) — sweep must NOT count this as a missing-symbol
    # observation. Marker must persist across BOTH cycles.
    state_store.set_positions("A1", [])
    # Intentionally NOT calling _mark_positions_sync_fresh here.
    await engine.evaluate_runners([_runner("t-1", "A1")])
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "stale positions sync must NOT trigger marker clearance "
        "(#236 round-4 P2)"
    )


class _RouterFilledResponse:
    """Router returning FILLED — synchronous terminal fill."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return (
            "hub-order-id",
            SimpleNamespace(status="FILLED", broker_order_id="BOI-FILLED"),
        )


@pytest.mark.asyncio
async def test_synchronous_fill_logs_submitted_event_not_rejected(caplog):
    """Round-4 P3: a synchronous terminal FILL must be reported as
    SUBMITTED (with synchronous_fill=True), not as a rejection.
    Mislabelling real executions as router rejections corrupts log
    review and metrics."""
    import logging as _logging
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterFilledResponse()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    with caplog.at_level(_logging.WARNING, logger="app.hub.exit_engines"):
        _seed_ltp("NG22MAY26255CE", 16.27)
        await engine.evaluate_runners([_runner("t-1", "A1")])
    submitted_records = [
        r for r in caplog.records
        if "POSITION_TRAILING_LOCK_EXIT_SUBMITTED" in r.getMessage()
    ]
    rejected_records = [
        r for r in caplog.records
        if "POSITION_TRAILING_LOCK_EXIT_REJECTED_BY_ROUTER" in r.getMessage()
    ]
    assert submitted_records, (
        "synchronous fill must emit SUBMITTED event (#236 round-4 P3)"
    )
    assert not rejected_records, (
        "synchronous fill must NOT be mislabelled as REJECTED_BY_ROUTER "
        "(#236 round-4 P3)"
    )


@pytest.mark.asyncio
async def test_marker_cleared_for_full_status():
    """Round-4 P2: ``FULL`` is a terminal-fill alias accepted by the
    canonical broker-status classifier — local prefix list must
    include it so a synchronous fill clears the marker."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterFullResponse()
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
    assert key not in engine._inflight_markers, (
        "FULL terminal fill must clear marker (#236 round-4 P2)"
    )


@pytest.mark.asyncio
async def test_marker_cleared_for_executed_status():
    """Round-4 P2: ``EXECUTED`` is also a canonical terminal-fill
    alias — local prefix list must include it."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterExecutedResponse()
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
    assert key not in engine._inflight_markers, (
        "EXECUTED terminal fill must clear marker (#236 round-4 P2)"
    )


class _RouterRaisesAfterDelay:
    """Router that raises an exception — simulates a slow submit_order
    that fails after potentially placing the broker order."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        raise RuntimeError("simulated broker timeout after submission")


@pytest.mark.asyncio
async def test_exception_path_refreshes_marker_timestamp():
    """Round-4 P2: when ``submit_order`` raises, the marker must be
    REFRESHED (timestamp reset) — not left armed at its pre-submit
    timestamp. A slow broker call that raises near the inflight
    timeout would otherwise leave a near-stale marker that the next
    watchdog cycle immediately treats as expired, allowing a duplicate
    submission against an already-placed order."""
    import time as _t
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterRaisesAfterDelay()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
    )
    # First evaluate: arm peak.
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # Second evaluate: giveback breached → _emit_exit fires; router
    # raises — marker is armed pre-submit (line 2537) then exception
    # path refreshes the timestamp.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers, (
        "marker must remain armed after router exception (#236 review P1)"
    )
    # Manually rewind the marker timestamp to simulate "marker has
    # been sitting around almost the full inflight window because the
    # submit was slow".
    pre_existing = engine._inflight_markers[key]
    rewound_ts = _t.monotonic() - 100.0  # well past 60s default
    engine._inflight_markers[key] = (pre_existing[0], rewound_ts)
    # Third evaluate: another exception — exception path must refresh
    # the timestamp rather than leave the rewound stale value in place.
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "marker must remain armed after router exception (#236 review P1)"
    )
    _, refreshed_ts = engine._inflight_markers[key]
    assert refreshed_ts > rewound_ts + 90.0, (
        "exception path must refresh marker timestamp so a slow submit "
        "does not leave a near-stale marker (#236 round-4 P2)"
    )


@pytest.mark.asyncio
async def test_single_missing_snapshot_does_not_clear_marker():
    """Round-4 P1: a single OK-but-empty broker poll is ambiguous
    (transient broker glitch can produce one). The marker must
    persist across a single missing snapshot and only clear after
    two consecutive missing observations."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-1")
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
    assert key in engine._inflight_markers
    _age_inflight_markers_past_grace(engine)
    # Single transient empty snapshot must NOT clear marker.
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "single missing snapshot is ambiguous — marker must persist "
        "(#236 round-4 P1)"
    )
    # If the symbol reappears, the miss-counter must reset.
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "marker persists when symbol reappears (#236 round-4 P1)"
    )
    # Now disappear again — counter restarts at 1, still no clearance.
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "miss-counter must reset on observed reappearance — single "
        "subsequent miss must not clear (#236 round-4 P1)"
    )


# ---------------------------------------------------------------------------
# Codex round-5 review on PR #236.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_evaluation_of_same_snapshot_does_not_count_as_misses():
    """Round-5 P2: the disappeared-symbol miss-counter must only
    increment on DISTINCT successful syncs (different ``last_ok_ts``).
    With ``HUB_SUBSCRIPTION_POLL_INTERVAL`` smaller than the broker
    positions sync interval, two watchdog ticks can observe the same
    snapshot before another sync runs — counting both as missing
    observations would clear the marker without a fresh broker
    confirmation."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-1")
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
    assert key in engine._inflight_markers
    _age_inflight_markers_past_grace(engine)
    state_store.set_positions("A1", [])
    # Mark sync fresh ONCE — both subsequent evaluations see the SAME
    # ``last_ok_ts`` fingerprint, so only ONE miss should be counted.
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "two evaluations of the SAME snapshot must not reach the "
        "two-miss threshold — distinct successful syncs are required "
        "(#236 round-5 P2)"
    )


@pytest.mark.asyncio
async def test_non_ok_status_suppresses_disappeared_sweep():
    """Round-5 P2: when positions sync status is not OK (e.g. ERROR
    or BLOCKED), the disappeared-symbol sweep must NOT count
    evaluations as misses. ``StateStore.update_positions_status``
    keeps the OLD ``last_ok_ts`` when a later sync reports ERROR, so
    timestamp-only freshness can be misleading after a failed sync."""
    from datetime import datetime, timezone
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-1")
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
    assert key in engine._inflight_markers
    _age_inflight_markers_past_grace(engine)
    # Empty snapshot but status reports ERROR — even with a recent
    # last_ok_ts, the sweep must NOT count this evaluation.
    state_store.set_positions("A1", [])
    state_store.update_positions_status(
        "A1",
        status="ERROR",
        last_ok_ts=datetime.now(timezone.utc).isoformat(),
        last_count=None,
        error_reason="broker_5xx",
        blocked_ts=None,
        retry_after_seconds=None,
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "non-OK status must suppress the disappeared-symbol sweep "
        "(#236 round-5 P2)"
    )


class _RouterCancelSingularResponse:
    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return ("hub-order-id", SimpleNamespace(status="CANCEL", broker_order_id=""))


@pytest.mark.asyncio
async def test_marker_cleared_for_singular_cancel_status():
    """Round-5 P2: the canonical ``classify_broker_status`` maps the
    bare status ``CANCEL`` to a terminal cancelled state; the local
    prefix list must include it too so a CANCEL response clears the
    marker rather than leaving it armed until timeout."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterCancelSingularResponse()
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
    assert key not in engine._inflight_markers, (
        "singular CANCEL status must clear marker (#236 round-5 P2)"
    )


class _RouterCancelPendingResponse:
    """Router returning CANCEL REQUESTED — pending cancellation,
    NOT a terminal state. Marker must remain armed."""

    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        return (
            "hub-order-id",
            SimpleNamespace(status="CANCEL REQUESTED", broker_order_id="BOI-PEND"),
        )


@pytest.mark.asyncio
async def test_pending_cancel_does_not_clear_marker():
    """Round-6 P2: ``CANCEL REQUESTED`` / ``CANCEL_PENDING`` are
    non-terminal pending cancellations (canonical
    ``classify_broker_status`` maps them to ``CANCEL_REQUESTED``).
    The marker MUST remain armed — clearing it would let the next
    watchdog cycle submit another trailing-lock exit while the
    original order may still be live or fill."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterCancelPendingResponse()
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
    assert key in engine._inflight_markers, (
        "pending CANCEL REQUESTED must keep marker armed (#236 round-6 P2)"
    )


class _RouterPaddedFilledResponse:
    def __init__(self):
        self.calls: List[dict] = []

    async def submit_order(
        self, *, tenant_id, broker_account_id, strategy_id, order_req,
    ):
        self.calls.append({"symbol": order_req.symbol})
        # Padded with whitespace — must still match terminal-fill prefix.
        return ("hub-order-id", SimpleNamespace(status=" FILLED", broker_order_id="BOI-1"))


@pytest.mark.asyncio
async def test_padded_terminal_status_strips_before_matching():
    """Round-5 P2: broker statuses with leading/trailing whitespace
    (e.g. `` FILLED``) must match the canonical terminal classifier
    by stripping before upper-casing."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterPaddedFilledResponse()
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
    assert key not in engine._inflight_markers, (
        "padded FILLED status must strip before matching (#236 round-5 P2)"
    )


@pytest.mark.asyncio
async def test_synchronous_fill_resets_persisted_trailing_state():
    """Round-5 P2: when a synchronous terminal fill clears the
    inflight marker, the persisted ``PositionTrailingLockManager``
    state must ALSO be reset. OrderLifecycleService projects the
    fill into the state store immediately, so the disappeared-symbol
    sweep never sees the position again to do this cleanup."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    router = _RouterFilledResponse()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    state_key = ("t-1", "A1", "NG22MAY26255CE")
    assert state_key in manager._states, "manager state present pre-fill"
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert state_key not in manager._states, (
        "manager state must be reset on synchronous fill so a "
        "same-symbol re-open does not inherit stale peak/armed "
        "(#236 round-5 P2)"
    )
