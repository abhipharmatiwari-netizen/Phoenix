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
    from datetime import datetime, timedelta, timezone
    rewound = _t.monotonic() - 10.0  # well past the 5s grace
    rewound_wall = datetime.now(timezone.utc) - timedelta(seconds=10)
    for key, value in list(engine._inflight_markers.items()):
        broker_order_id = value[0]
        engine._inflight_markers[key] = (broker_order_id, rewound, rewound_wall)


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


def _seed_terminal_broker_order(
    state_store: StateStore,
    account_id: str,
    *,
    symbol: str,
    broker_order_id: str,
    status: str = "FILLED",
    quantity: int = 1250,
) -> None:
    """Issue #252: seed a terminal broker-order entry into state_store so
    the disappeared-symbol sweep can confirm explicit terminal evidence
    before clearing the inflight marker."""
    from app.brokers.base import OrderStatus

    order = OrderStatus(
        order_id=broker_order_id,
        symbol=symbol,
        side="SELL",
        status=status,
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=quantity,
        filled_quantity=quantity if status.upper() == "FILLED" else 0,
    )
    state_store.set_orders(account_id, [order])


@pytest.mark.asyncio
async def test_marker_cleared_when_symbol_disappears_from_snapshot():
    """Codex P2 round 1: state stores can omit closed positions from
    the snapshot entirely. When a fill removes the symbol from the
    next snapshot, the marker must be swept (after grace period).

    PR #236 round-4 review P1: clearing now requires TWO consecutive
    missing snapshots — a single OK-but-empty broker poll is
    ambiguous and must not flush the duplicate-fill guard. This test
    therefore advances the empty-snapshot evaluation twice.

    Issue #252 (PR #248-252): in addition, the sweep now requires
    explicit terminal broker-order evidence before clearing — so this
    test ALSO seeds a FILLED order for the marker's broker_order_id."""
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
    _seed_terminal_broker_order(
        state_store, "A1",
        symbol="NG22MAY26255CE", broker_order_id="BOI-1",
        status="FILLED",
    )
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
        "+ terminal broker evidence (#252)"
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
    # Issue #252: the sweep also requires terminal broker-order evidence
    # so a FILLED ledger entry is now part of the precondition.
    state_store.set_positions("A1", [])
    # The default router (_RouterReturningBrokerOrderId) puts the broker
    # order id in the marker — match it so the terminal-evidence gate
    # passes.
    armed_marker = engine._inflight_markers.get(state_key)
    armed_boi = armed_marker[0] if armed_marker else None
    if armed_boi is None:
        armed_boi = "BOI-TEST"
    _seed_terminal_broker_order(
        state_store, "A1",
        symbol="NG22MAY26255CE", broker_order_id=str(armed_boi),
        status="FILLED",
    )
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])  # 1st miss
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])  # 2nd miss

    assert state_key not in manager._states, (
        "manager peak/armed state must be reset when symbol disappears "
        "across two consecutive snapshots WITH terminal broker evidence "
        "(#236 round-2 P1, round-4 P1, #252)"
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
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    rewound_wall = _dt.now(_tz.utc) - _td(seconds=100)
    engine._inflight_markers[key] = (
        pre_existing[0], rewound_ts, rewound_wall,
    )
    # Third evaluate: another exception — exception path must refresh
    # the timestamp rather than leave the rewound stale value in place.
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "marker must remain armed after router exception (#236 review P1)"
    )
    _, refreshed_ts, _wall = engine._inflight_markers[key]
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


# ===========================================================================
# PR for issues #248, #249, #250, #251, #252 — trailing-lock marker
# lifecycle hardening.
# ===========================================================================


class _FakeInflightBackend:
    """In-memory test double for ``PositionTrailingLockInflightBackend``.

    Records save/delete/load_all calls so we can verify the engine's
    durable-marker handling without touching Postgres. Used for the
    restart-survives-marker test (issue #251).
    """

    def __init__(self):
        from app.pnl.position_trailing_lock import (
            PositionTrailingLockInflightMarker,
        )
        self._rows: dict[tuple, "PositionTrailingLockInflightMarker"] = {}
        self.save_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []
        self.load_calls: int = 0
        # PR #261 round-2: record the ``raise_on_failure`` flag the
        # engine passed so tests can assert LIVE call sites opted in.
        self.save_raise_on_failure: list[bool] = []
        self.delete_raise_on_failure: list[bool] = []
        self.load_raise_on_failure: list[bool] = []

    def save_marker(self, marker, *, raise_on_failure: bool = False) -> None:
        key = (marker.tenant_id, marker.broker_account_id, marker.symbol)
        self._rows[key] = marker
        self.save_calls.append(key)
        self.save_raise_on_failure.append(bool(raise_on_failure))

    def delete_marker(
        self, tenant_id, broker_account_id, symbol,
        *, raise_on_failure: bool = False,
    ) -> None:
        key = (str(tenant_id), str(broker_account_id), str(symbol))
        self._rows.pop(key, None)
        self.delete_calls.append(key)
        self.delete_raise_on_failure.append(bool(raise_on_failure))

    def load_all(self, *, raise_on_failure: bool = False):
        self.load_calls += 1
        self.load_raise_on_failure.append(bool(raise_on_failure))
        return list(self._rows.values())


@pytest.mark.asyncio
async def test_issue_251_marker_persists_to_durable_backend_on_submit():
    """Issue #251: every submit must write a durable row so a restart
    between submit and broker terminal confirmation does NOT drop the
    duplicate-fill guard."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-251")
    backend = _FakeInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    expected_key = ("t-1", "A1", "NG22MAY26255CE")
    assert expected_key in engine._inflight_markers
    assert expected_key in backend.save_calls, (
        "submit must persist the inflight marker to the durable backend "
        "so a restart does not drop the duplicate-fill guard (#251)"
    )


@pytest.mark.asyncio
async def test_issue_251_restart_rehydrates_marker_and_blocks_duplicate():
    """Issue #251: a NEW engine instance constructed against a backend
    that already has a persisted marker must rehydrate the in-memory
    dict and block a duplicate submission on the same (tenant, account,
    symbol). This simulates the restart case."""
    from app.pnl.position_trailing_lock import PositionTrailingLockInflightMarker
    from datetime import datetime, timezone

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    backend = _FakeInflightBackend()
    backend.save_marker(
        PositionTrailingLockInflightMarker(
            tenant_id="t-1",
            broker_account_id="A1",
            symbol="NG22MAY26255CE",
            broker_order_id="BOI-PRE-RESTART",
            submitted_at=datetime.now(timezone.utc),
        )
    )
    # save_calls also recorded the pre-load above — clear it so we test
    # the rehydrate-block flow strictly.
    backend.save_calls.clear()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-NEW")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert backend.load_calls >= 1, (
        "engine must load persisted markers on the first evaluate (#251)"
    )
    assert router.calls == [], (
        "rehydrated marker must block the duplicate submission across "
        "a simulated restart (#251)"
    )


@pytest.mark.asyncio
async def test_issue_251_clear_inflight_deletes_durable_row():
    """Issue #251: when the marker is cleared (router rejection,
    synchronous fill, terminal confirmation), the durable row must
    also be deleted so a subsequent restart does NOT rehydrate it."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterRejectingResponse()
    backend = _FakeInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    expected_key = ("t-1", "A1", "NG22MAY26255CE")
    assert expected_key in backend.delete_calls, (
        "router rejection must delete the persisted row so a restart "
        "does not rehydrate a stale marker (#251)"
    )


@pytest.mark.asyncio
async def test_issue_249_post_submit_router_failure_persists_marker():
    """Issue #249: when the router places the broker order and then
    raises (e.g. lifecycle persistence fails after broker submit), the
    marker must remain armed IN MEMORY and on the durable backend."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterRaisingPostSubmit()
    backend = _FakeInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    expected_key = ("t-1", "A1", "NG22MAY26255CE")
    assert expected_key in engine._inflight_markers, (
        "post-submit raise must leave the in-memory marker armed (#249)"
    )
    assert expected_key in backend.save_calls, (
        "post-submit raise must leave the durable marker armed so a "
        "restart between submit and broker terminal does not allow a "
        "duplicate submission (#249 + #251)"
    )
    assert expected_key not in backend.delete_calls, (
        "post-submit raise MUST NOT clear the durable marker (#249)"
    )


@pytest.mark.asyncio
async def test_issue_252_disappearance_alone_does_not_clear_marker():
    """Issue #252: a position vanishing from the snapshot is NOT proof
    of terminal state. Without explicit terminal broker-order evidence,
    the marker must persist until the normal inflight timeout — even
    after two consecutive missing snapshots."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-252")
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
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key in engine._inflight_markers, (
        "marker must persist when symbol disappears WITHOUT terminal "
        "broker-order evidence — disappearance alone is not terminal "
        "proof (#252)"
    )


@pytest.mark.asyncio
async def test_issue_252_disappearance_plus_terminal_evidence_clears_marker():
    """Issue #252: when the disappeared-symbol sweep CAN confirm
    terminal broker-order evidence (e.g. a FILLED ledger entry for the
    marker's broker_order_id), it is safe to clear the marker."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-TERMINAL")
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
    _seed_terminal_broker_order(
        state_store, "A1",
        symbol="NG22MAY26255CE", broker_order_id="BOI-TERMINAL",
        status="FILLED",
    )
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert key not in engine._inflight_markers, (
        "with terminal broker evidence + 2 missing snapshots, the "
        "marker may be cleared (#252)"
    )


@pytest.mark.asyncio
async def test_issue_250_manager_state_reset_only_when_terminal_evidence():
    """Issue #250: when a symbol disappears from the snapshot, the
    persisted ``PositionTrailingLockManager`` state must NOT be reset
    unless we also have explicit terminal broker-order evidence.
    Otherwise an OK-but-empty snapshot can blow away a valid armed
    peak/state that should still be governing the (still-open) position
    on the next snapshot."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-250")
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
    assert state_key in manager._states
    _age_inflight_markers_past_grace(engine)
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert state_key in manager._states, (
        "manager state must NOT be reset on disappearance WITHOUT "
        "terminal broker evidence (#250)"
    )


def _live_settings_namespace(
    *,
    inflight_max_seconds: float,
    poll_interval: float = 60.0,
) -> SimpleNamespace:
    """Build a Settings-like SimpleNamespace that passes the LIVE
    startup validator EXCEPT for the trailing-lock inflight gate.
    Other LIVE-required flags are stubbed to acceptable values."""
    return SimpleNamespace(
        eod_exit_time="15:20",
        eod_exit_retry_cutoff_time="15:30",
        hub_subscription_poll_interval=poll_interval,
        position_trailing_lock_enabled=True,
        position_trailing_lock_inflight_max_seconds=inflight_max_seconds,
        risk_enable_daily_loss=True,
        risk_max_daily_loss=10000.0,
        eod_cancel_open_orders_enabled=True,
        admin_api_key="X",
        dashboard_hmac_auth_enabled=False,
        control_plane_backend="postgres",
        sweep_state_backend="postgres",
        broker_secret_backend="postgres",
        enable_capital_checks=True,
        enable_risk_checks=True,
        enable_profit_checks=True,
        profit_enable_daily_target=True,
        order_router_enforce_idempotency=True,
        position_ownership_enabled=True,
        order_lifecycle_persist_markers_required=True,
        capital_fail_closed_on_missing_state=True,
        capital_fail_closed_on_missing_notional_price=True,
        capital_limits_json='{"A":{}}',
        allow_live_capital_limits_default_only="",
        risk_fail_open_on_missing_pnl=False,
        position_ownership_unknown_mode="block_entries",
        ownership_persist_pending_locks=True,
        profit_daily_target=2000.0,
        circuit_breaker_persist_state=True,
        hub_instance_name="phoenix-live",
        hub_default_tenant_id="tenant-1",
        dashboard_auth_disabled=False,
        angel_postback_token="x",
        default_profit_target_pct=0.3,
        enable_multi_hub=True,
        use_hub_router=True,
        sweep_state_db_dsn="",
    )


def _live_runtime_cfg_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        stream_watchdog_interval_seconds=5.0,
        stream_watchdog_restart_backoff_base_seconds=1.0,
        stream_watchdog_restart_backoff_max_seconds=60.0,
        stream_watchdog_restart_backoff_jitter_ratio=0.1,
        stream_watchdog_stable_run_window_seconds=10.0,
        app_env="production",
        leader_lease_backend="postgres",
        leader_lease_enabled_override=True,
        leader_lease_id="phoenix-live",
        disable_stream_worker=False,
        demo_auth_requested=False,
        enable_demo_auth=False,
        dashboard_auth_disabled=False,
    )


_LIVE_ENV_BASE = {
    "APP_ENV": "production",
    "TRADE_MODE": "LIVE",
    "POSITION_SYNC_INTERVAL_SECONDS": "30",
    "ORDERS_SYNC_INTERVAL_SECONDS": "90",
    "ORDER_SUBMISSION_OUTBOX_ENABLED": "true",
    "ORDER_SUBMISSION_OUTBOX_REQUIRED": "true",
    "ORDER_SUBMISSION_OUTBOX_BACKEND": "postgres",
    "BROKER_SECRET_BACKEND": "postgres",
    "PNL_STATE_BACKEND": "postgres",
    "RISK_MAX_DAILY_LOSS_LIVE_FLOOR": "5000",
    "ENABLE_DEMO_AUTH": "false",
}


def test_issue_248_startup_validator_rejects_inflight_below_floor_in_live():
    """Issue #248: LIVE startup must abort when
    POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS is at or below the
    effective watchdog cadence + safety margin. Mirrors the
    RISK_MAX_DAILY_LOSS_LIVE_FLOOR pattern added by PR #243."""
    from app.core.startup_config_validator import (
        validate_runtime_startup_settings,
    )

    # 60s = exact effective cadence — under the +30s LIVE floor.
    with pytest.raises(ValueError) as excinfo:
        validate_runtime_startup_settings(
            settings=_live_settings_namespace(inflight_max_seconds=60.0),
            runtime_cfg=_live_runtime_cfg_namespace(),
            env=dict(_LIVE_ENV_BASE),
        )
    err = str(excinfo.value)
    assert "POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS" in err, err
    assert "#248" in err or "issues #225 + #248" in err, err


def test_issue_248_startup_validator_rejects_zero_inflight_in_live():
    """Issue #248: LIVE startup must abort when
    POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS=0 (silently disables
    the duplicate-fill guard)."""
    from app.core.startup_config_validator import (
        validate_runtime_startup_settings,
    )

    with pytest.raises(ValueError) as excinfo:
        validate_runtime_startup_settings(
            settings=_live_settings_namespace(inflight_max_seconds=0.0),
            runtime_cfg=_live_runtime_cfg_namespace(),
            env=dict(_LIVE_ENV_BASE),
        )
    err = str(excinfo.value)
    assert "POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS" in err, err


def test_issue_248_startup_validator_accepts_inflight_above_floor_in_live():
    """Issue #248: LIVE startup must accept a properly-sized
    POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS (default 120s is well
    above the 60+30=90s floor)."""
    from app.core.startup_config_validator import (
        validate_runtime_startup_settings,
    )

    try:
        validate_runtime_startup_settings(
            settings=_live_settings_namespace(inflight_max_seconds=120.0),
            runtime_cfg=_live_runtime_cfg_namespace(),
            env=dict(_LIVE_ENV_BASE),
        )
    except ValueError as exc:
        # If the validator raises for an unrelated gate (e.g. broker
        # network identity), that's not what this test cares about —
        # just assert the trailing-lock gate is NOT part of the error.
        assert "POSITION_TRAILING_LOCK_INFLIGHT_MAX_SECONDS" not in str(exc), (
            f"trailing-lock gate must accept 120s in LIVE; got: {exc}"
        )


# ---------------------------------------------------------------------------
# PR #261 round-1 Codex review fixes — five findings on PR #261 itself.
# Tests below verify each fix individually.
# ---------------------------------------------------------------------------


class _FailingThenSucceedingInflightBackend(_FakeInflightBackend):
    """Backend that raises on the first ``load_all`` then succeeds.

    Models a transient Postgres outage on the first eval after restart —
    the previous engine implementation set ``_inflight_hydrated = True``
    before calling ``load_all`` so the second-cycle retry never happened.
    """

    def __init__(self):
        super().__init__()
        self._load_should_fail = True
        self._raised_loads = 0

    def load_all(self, *, raise_on_failure: bool = False):
        self.load_calls += 1
        self.load_raise_on_failure.append(bool(raise_on_failure))
        if self._load_should_fail:
            self._raised_loads += 1
            self._load_should_fail = False  # only fail once
            raise RuntimeError("simulated postgres outage on first load")
        return list(self._rows.values())


@pytest.mark.asyncio
async def test_pr261_p1_hydrate_retries_after_load_failure():
    """PR #261 round-1 P1: when the first ``load_all`` raises, the
    engine must NOT mark itself as hydrated; the next eval cycle must
    retry and (on success) flip the hydrated flag. Previously the flag
    was set BEFORE the call so a transient Postgres outage permanently
    disabled the duplicate-fill guard for the lifetime of the
    process."""
    from app.pnl.position_trailing_lock import PositionTrailingLockInflightMarker
    from datetime import datetime, timezone

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    backend = _FailingThenSucceedingInflightBackend()
    # Seed a marker so we can prove the second hydrate actually loaded
    # rows after the first attempt raised.
    backend.save_marker(
        PositionTrailingLockInflightMarker(
            tenant_id="t-1",
            broker_account_id="A1",
            symbol="NG22MAY26255CE",
            broker_order_id="BOI-261-P1A",
            submitted_at=datetime.now(timezone.utc),
        )
    )
    backend.save_calls.clear()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-NEW")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )

    # Cycle 1 — load_all raises. Engine must still be NOT hydrated and
    # the failed-attempt counter must have advanced. No exception is
    # propagated to the watchdog (degraded service is preferable to a
    # crashed engine).
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert backend._raised_loads == 1
    assert engine._inflight_hydrated is False, (
        "first hydrate raised — must NOT flip to True or the duplicate-"
        "fill guard stays permanently degraded (PR #261 round-1 P1)"
    )
    assert engine._inflight_hydrate_failed_attempts >= 1, (
        "failed-attempt counter must advance on each retry so an "
        "operator can see how long the guard has been degraded"
    )

    # Cycle 2 — load_all succeeds. Engine flips to hydrated and the
    # persisted marker (key) is loaded into memory.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert engine._inflight_hydrated is True, (
        "successful retry must flip hydrated=True"
    )
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers, (
        "rehydrated marker must be present in memory after successful retry"
    )
    # The rehydrated marker MUST block the duplicate submission that
    # the cycle-2 pullback would otherwise produce.
    assert router.calls == [], (
        "rehydrated marker must block the duplicate exit submission "
        "(PR #261 round-1 P1)"
    )


class _PersistFailingInflightBackend(_FakeInflightBackend):
    """Backend whose ``save_marker`` raises — models a Postgres outage
    during the pre-submit UPSERT in LIVE."""

    def save_marker(self, marker, *, raise_on_failure: bool = False) -> None:
        self.save_calls.append(
            (marker.tenant_id, marker.broker_account_id, marker.symbol)
        )
        self.save_raise_on_failure.append(bool(raise_on_failure))
        raise RuntimeError("simulated postgres outage on save")


@pytest.mark.asyncio
async def test_pr261_p1_pre_submit_persist_failure_in_live_aborts_submission(monkeypatch):
    """PR #261 round-1 P1: pre-submit UPSERT failure in LIVE must
    ABORT the broker submission. Previously the failure was logged and
    swallowed; ``_emit_exit()`` then placed the broker order with no
    durable guard, and a restart between submit and confirmation would
    drop the guard."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-WOULD-FILL")
    backend = _PersistFailingInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert router.calls == [], (
        "pre-submit persist failure in LIVE must ABORT the broker "
        "submission to avoid running without the durable duplicate-"
        "fill guard (PR #261 round-1 P1)"
    )
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key not in engine._inflight_markers, (
        "in-memory marker must be rolled back on abort so a subsequent "
        "eval (after Postgres is restored) can re-arm cleanly"
    )


@pytest.mark.asyncio
async def test_pr261_p1_pre_submit_persist_failure_in_paper_still_submits(monkeypatch):
    """PR #261 round-1 P1: PAPER/SHADOW preserve the historical log-only
    fallback — no real broker order is at stake so a persist failure
    must NOT abort submission."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-PAPER")
    backend = _PersistFailingInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert len(router.calls) == 1, (
        "PAPER mode must preserve historical log-only behaviour — a "
        "persist failure should not abort the broker submission"
    )


@pytest.mark.asyncio
async def test_pr261_p2_disappeared_no_evidence_ages_out_after_max_seconds():
    """PR #261 round-1 P2: a marker for a symbol that disappeared from
    the snapshot WITHOUT terminal broker-order evidence must be aged
    out once it is older than ``inflight_max_seconds`` — otherwise it
    lives forever and emits HELD_NO_TERMINAL every watchdog cycle."""
    import time as _t
    from datetime import datetime, timedelta, timezone

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-P2")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=120.0,
        ),
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

    # Rewind the marker so its monotonic age exceeds the configured
    # ``inflight_max_seconds`` (120s here). NO terminal ledger evidence
    # is seeded — the sweep must rely on the age-out path.
    rewound = _t.monotonic() - 200.0
    rewound_wall = datetime.now(timezone.utc) - timedelta(seconds=200)
    broker_order_id = engine._inflight_markers[key][0]
    engine._inflight_markers[key] = (broker_order_id, rewound, rewound_wall)

    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key not in engine._inflight_markers, (
        "a marker older than inflight_max_seconds with no terminal "
        "evidence and a disappeared symbol must be aged out — "
        "otherwise it lives forever (PR #261 round-1 P2)"
    )


@pytest.mark.asyncio
async def test_pr261_p2_terminal_evidence_reads_full_order_snapshot():
    """PR #261 round-1 P2: ``_marker_has_terminal_broker_evidence``
    must read the FULL broker snapshot via ``get_order_snapshot()``
    rather than the active-only ``get_orders()`` projection.
    AccountRunner sync writes only active orders to ``set_orders``;
    terminal rows live in ``set_order_snapshot``."""
    from app.brokers.base import OrderStatus

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-P2B")
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

    # Seed terminal evidence ONLY into the full broker snapshot
    # (set_order_snapshot), NOT into set_orders. The active-only
    # ``get_orders`` returns [] here — mirrors real AccountRunner
    # behaviour where the active projection drops terminal rows.
    state_store.set_positions("A1", [])
    terminal_order = OrderStatus(
        order_id="BOI-261-P2B",
        symbol="NG22MAY26255CE",
        side="SELL",
        status="FILLED",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1250,
        filled_quantity=1250,
    )
    state_store.set_order_snapshot("A1", [terminal_order])
    # Explicitly clear set_orders so the test is unambiguous: only the
    # snapshot has the terminal evidence.
    state_store.set_orders("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key not in engine._inflight_markers, (
        "marker must clear when terminal evidence exists in "
        "get_order_snapshot() — even if get_orders() is empty "
        "(PR #261 round-1 P2)"
    )


@pytest.mark.asyncio
async def test_pr261_p2_cancel_pending_in_ledger_does_not_clear_marker():
    """PR #261 round-1 P2: ``classify_broker_status`` fuzzy-maps any
    unrecognized status containing ``CANCEL`` (e.g. ``CANCEL_PENDING``
    with underscore — NOT in the canonical map) to ``CANCELLED``
    (terminal). The terminal-evidence helper must explicitly veto
    pending-cancel variants BEFORE delegating, otherwise a ledger
    reporting a pending cancel would clear the marker while the
    exit is actually still live."""
    from app.brokers.base import OrderStatus

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-P2C")
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

    # Seed a CANCEL_PENDING (with underscore — the exact variant the
    # canonical map misses and the fuzzy fallback mis-classifies) into
    # the broker order ledger.
    state_store.set_positions("A1", [])
    pending = OrderStatus(
        order_id="BOI-261-P2C",
        symbol="NG22MAY26255CE",
        side="SELL",
        status="CANCEL_PENDING",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1250,
        filled_quantity=0,
    )
    state_store.set_order_snapshot("A1", [pending])
    state_store.set_orders("A1", [pending])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key in engine._inflight_markers, (
        "CANCEL_PENDING is a non-terminal pending cancel — the marker "
        "must NOT be cleared by the terminal-evidence sweep "
        "(PR #261 round-1 P2)"
    )


# ---------------------------------------------------------------------------
# PR #261 round-2 review — the round-1 patches added engine-layer
# fail-closed gates, but Codex pointed out that the underlying backend
# was still silently swallowing Postgres failures so the gates never
# fired. The tests below pin the round-2 fixes in place:
#   * Backend exposes ``raise_on_failure`` (default False, opt-in True).
#   * Engine LIVE call sites use ``raise_on_failure=True``.
#   * Engine refuses to submit when not hydrated in LIVE.
#   * HubRuntime sets ``inflight_disabled=True`` if backend init fails
#     in LIVE — engine refuses ALL submissions for the process lifetime.
#   * Disappeared-marker timeout sweep also resets manager state.
#   * Unknown-id terminal-evidence fallback requires side / quantity /
#     timestamp match (no unrelated entry order satisfies the gate).
# ---------------------------------------------------------------------------


def test_pr261_round2_backend_raise_on_failure_signature():
    """Round-2 root cause: the protocol and the noop backend must
    accept ``raise_on_failure`` so LIVE call sites can opt into the
    fail-closed contract that the engine layer enforces."""
    import inspect
    from app.pnl.position_trailing_lock import (
        _NoopPositionTrailingLockInflightBackend,
    )
    backend = _NoopPositionTrailingLockInflightBackend()
    for method_name in ("save_marker", "delete_marker", "load_all"):
        sig = inspect.signature(getattr(backend, method_name))
        assert "raise_on_failure" in sig.parameters, (
            f"{method_name} must accept ``raise_on_failure`` so LIVE callers "
            "can opt into fail-closed (PR #261 round-2 root cause)"
        )
        assert (
            sig.parameters["raise_on_failure"].default is False
        ), (
            f"{method_name}.raise_on_failure default MUST be False so "
            "non-LIVE callers preserve the legacy log-and-continue path "
            "(PR #261 round-2)"
        )


@pytest.mark.asyncio
async def test_pr261_round2_engine_passes_raise_on_failure_true_for_live_paths(monkeypatch):
    """Round-2 wiring proof: in LIVE, every engine call into the
    backend MUST opt into raise_on_failure=True. Without this, the
    backend's exception handler would swallow Postgres failures and
    the engine-layer fail-closed gates would never fire."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2A")
    backend = _FakeInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert backend.load_raise_on_failure, "load_all must have been called"
    assert all(backend.load_raise_on_failure), (
        "every load_all() call in LIVE MUST use raise_on_failure=True "
        "(round-2 root cause)"
    )
    assert backend.save_raise_on_failure, "save_marker must have been called"
    # Pre-submit save uses raise_on_failure=True; the post-submit refresh
    # path uses False (broker order already placed).
    assert backend.save_raise_on_failure[0] is True, (
        "pre-submit save_marker in LIVE MUST use raise_on_failure=True "
        "(round-2 root cause)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_live_skips_submission_when_hydrate_pending(monkeypatch):
    """P1 (exit_engines.py:2405): in LIVE, if the durable hydrate has
    not succeeded (Postgres still down), the engine MUST refuse to
    submit any trailing-lock exits this cycle. An empty in-memory
    marker set is NOT proof "no exits are in-flight" — it is "we
    could not load the durable record"."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2B")

    class _AlwaysFailingHydrateBackend(_FakeInflightBackend):
        def load_all(self, *, raise_on_failure: bool = False):
            self.load_calls += 1
            self.load_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated permanent postgres outage")

    backend = _AlwaysFailingHydrateBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    # Force an arming condition — pullback after peak.
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert engine._inflight_hydrated is False, (
        "permanent backend failure must NOT flip hydrated to True"
    )
    assert router.calls == [], (
        "LIVE must NOT submit trailing-lock exits while hydrate is "
        "pending — submitting with unknown durable state could "
        "duplicate-fill (PR #261 round-2 P1 — exit_engines.py:2405)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_paper_continues_when_hydrate_pending(monkeypatch):
    """The LIVE-only hydrate gate must NOT block PAPER/SHADOW
    submissions — no real broker order is at stake there, and the
    historical best-effort semantics are preserved so dev loops are
    not impacted by transient infrastructure issues."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2C")

    class _AlwaysFailingHydrateBackend(_FakeInflightBackend):
        def load_all(self, *, raise_on_failure: bool = False):
            self.load_calls += 1
            self.load_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated postgres outage")

    backend = _AlwaysFailingHydrateBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls, (
        "PAPER must continue submitting trailing-lock exits when the "
        "durable backend is unavailable (best-effort semantics)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_inflight_disabled_blocks_all_submissions():
    """P1 (runtime.py:539): when HubRuntime detects backend init
    failure in LIVE it sets ``inflight_disabled=True`` on the engine.
    The engine MUST refuse to submit any trailing-lock exit until a
    process restart re-attempts the backend init."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2D")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_disabled=True,
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], (
        "engine MUST refuse all submissions when inflight_disabled "
        "is set (PR #261 round-2 P1 — runtime.py:539)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_disappeared_timeout_resets_manager_state():
    """P2 (exit_engines.py:2834): when the disappeared-marker
    timeout fires WITHOUT terminal evidence, the engine must also
    reset ``PositionTrailingLockManager`` state for the symbol —
    mirroring the terminal-evidence branch — so a quick same-symbol
    re-open is evaluated against a fresh peak (issue #250)."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2E")
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=0.0,
        ),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers
    # Verify the manager has armed (peak captured) for the symbol.
    armed_state = manager._states.get(("t-1", "A1", "NG22MAY26255CE"))
    assert armed_state is not None and armed_state.peak_unrealized_pnl > 0.0, (
        "test scaffolding bug: manager peak must be armed before the "
        "disappeared-timeout sweep is exercised"
    )
    _age_inflight_markers_past_grace(engine)

    # Symbol vanishes from the snapshot. No terminal ledger evidence.
    state_store.set_positions("A1", [])
    state_store.set_orders("A1", [])
    state_store.set_order_snapshot("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key not in engine._inflight_markers, (
        "marker must have aged out after the disappeared-symbol "
        "timeout (already tested in round-1)"
    )
    assert ("t-1", "A1", "NG22MAY26255CE") not in manager._states, (
        "manager state for the symbol MUST be reset on disappeared-"
        "marker timeout (PR #261 round-2 P2 — exit_engines.py:2834)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_unknown_id_fallback_rejects_unrelated_entry_order():
    """P2 (exit_engines.py:3047): when the router did not surface a
    broker_order_id and the disappeared-symbol fallback looks at the
    last ledger row for the symbol, an older filled ENTRY order
    (BUY side, filled BEFORE the marker was armed) MUST NOT satisfy
    the terminal-evidence gate."""
    from app.brokers.base import OrderStatus

    class _NoOrderIdRouter:
        def __init__(self):
            self.calls = []

        async def submit_order(
            self, *, tenant_id, broker_account_id, strategy_id, order_req,
        ):
            self.calls.append(order_req)
            # Router accepted, but no broker_order_id surfaced. This is
            # the exact case the fallback exists for.
            return ("hub-order-id", SimpleNamespace(status="OK"))

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    # Seed an OLDER filled BUY entry order — it precedes the marker.
    from datetime import datetime, timedelta, timezone
    entry_order = OrderStatus(
        order_id="BOI-OLDER-ENTRY",
        symbol="NG22MAY26255CE",
        side="BUY",  # opposite of the SELL exit
        status="FILLED",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1,  # also a different size
        filled_quantity=1,
        updated_at=(
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(),
    )
    state_store.set_orders("A1", [entry_order])
    state_store.set_order_snapshot("A1", [entry_order])
    router = _NoOrderIdRouter()
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
        "test scaffolding bug: marker must be armed before the "
        "fallback path is exercised"
    )
    _age_inflight_markers_past_grace(engine)

    # Symbol disappears. Ledger still has the OLDER entry order.
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key in engine._inflight_markers, (
        "an older unrelated BUY entry order MUST NOT satisfy the "
        "terminal-evidence gate for a SELL exit marker armed AFTER "
        "the entry (PR #261 round-2 P2 — exit_engines.py:3047)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_unknown_id_fallback_accepts_matching_exit_row():
    """P2 (exit_engines.py:3047): the round-2 stricter match MUST
    still ACCEPT a properly matching exit row — same symbol, opposite
    side (SELL for a long), matching lots quantity, and ``updated_at``
    after the marker was armed."""
    from app.brokers.base import OrderStatus

    class _NoOrderIdRouter:
        def __init__(self):
            self.calls = []

        async def submit_order(
            self, *, tenant_id, broker_account_id, strategy_id, order_req,
        ):
            self.calls.append(order_req)
            return ("hub-order-id", SimpleNamespace(status="OK"))

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _NoOrderIdRouter()
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
    spec = engine._inflight_exit_specs.get(key)
    # PR #261 round-3 review P2 (exit_engines.py:3283): the spec is
    # now captured in BROKER UNITS (1 lot of NG = 1250 units) so the
    # subsequent fallback compares against the broker ledger row's
    # ``OrderStatus.quantity``, which is also broker units after the
    # router rewrites quantity to ``broker_qty``.
    assert spec is not None and spec[0] == "SELL" and spec[1] == 1250, (
        "marker spec capture must record SELL side and 1250 broker "
        "units (1 lot * 1250 lot_size) for the subsequent fallback "
        "match (PR #261 round-3 review P2 — exit_engines.py:3283)"
    )
    _age_inflight_markers_past_grace(engine)

    # Symbol disappears AND a FILLED SELL row appears in the ledger,
    # quantity=1250 broker units (matches marker), updated_at AFTER
    # the marker armed.
    from datetime import datetime, timezone
    exit_row = OrderStatus(
        order_id="BOI-LATE-EXIT",
        symbol="NG22MAY26255CE",
        side="SELL",
        status="FILLED",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1250,
        filled_quantity=1250,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    state_store.set_orders("A1", [exit_row])
    state_store.set_order_snapshot("A1", [exit_row])
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key not in engine._inflight_markers, (
        "a matching SELL FILLED row with the right quantity and "
        "updated_at must satisfy the terminal-evidence gate"
    )


@pytest.mark.asyncio
async def test_pr261_round2_clear_inflight_marker_surfaces_delete_failure_in_live(monkeypatch):
    """P2 (position_trailing_lock.py:568): in LIVE, the engine
    MUST request ``raise_on_failure=True`` when deleting markers
    AND surface delete failures as a structured ERROR event. A
    swallowed delete leaves a durable row that a future restart
    would rehydrate as a stale marker."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2F")

    class _DeleteFailingBackend(_FakeInflightBackend):
        def delete_marker(
            self, tenant_id, broker_account_id, symbol,
            *, raise_on_failure: bool = False,
        ):
            self.delete_calls.append(
                (str(tenant_id), str(broker_account_id), str(symbol))
            )
            self.delete_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated postgres outage on delete")

    backend = _DeleteFailingBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    # Directly invoke the clear path to keep the test focused.
    engine._inflight_markers[("t-1", "A1", "S")] = (None, 0.0, None)
    engine._clear_inflight_marker(
        tenant_id="t-1", broker_account_id="A1", symbol="S",
    )
    assert backend.delete_raise_on_failure == [True], (
        "in LIVE, _clear_inflight_marker MUST request "
        "raise_on_failure=True (PR #261 round-2 P2 — "
        "position_trailing_lock.py:568)"
    )


@pytest.mark.asyncio
async def test_pr261_round2_clear_inflight_marker_paper_keeps_log_only(monkeypatch):
    """In PAPER/SHADOW, the legacy log-only delete is preserved so
    test fixtures and dev loops do not break on backend hiccups."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    state_store = StateStore()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R2G")

    class _DeleteCountingBackend(_FakeInflightBackend):
        pass

    backend = _DeleteCountingBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    engine._inflight_markers[("t-1", "A1", "S")] = (None, 0.0, None)
    engine._clear_inflight_marker(
        tenant_id="t-1", broker_account_id="A1", symbol="S",
    )
    assert backend.delete_raise_on_failure == [False], (
        "PAPER/SHADOW must keep raise_on_failure=False (legacy "
        "log-only delete preserved)"
    )


# ---------------------------------------------------------------------------
# PR #261 round-3 review: five verified-new P2 findings on head aefa6bb.
#   * Surface delete failures in the terminal-evidence clear path
#     (exit_engines.py:3072)
#   * Propagate stale-marker delete failures in LIVE
#     (exit_engines.py:2227)
#   * Persist the exit spec needed after unknown-id restarts
#     (exit_engines.py:2509)
#   * Keep cleanup running when the inflight backend is disabled
#     (exit_engines.py:2584)
#   * Compare exit rows using broker units, not lots
#     (exit_engines.py:3283)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr261_round3_stale_marker_timeout_surfaces_delete_failure_in_live(
    monkeypatch,
):
    """P2 (exit_engines.py:2227): the stale-marker timeout clear path
    in ``_is_inflight_blocked`` MUST pass ``raise_on_failure=True`` to
    the backend in LIVE so a Postgres delete failure is surfaced via a
    structured ERROR event rather than silently leaving the durable
    row behind for a future restart to rehydrate."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3A")

    class _DeleteFailingBackend(_FakeInflightBackend):
        def delete_marker(
            self, tenant_id, broker_account_id, symbol,
            *, raise_on_failure: bool = False,
        ):
            self.delete_calls.append(
                (str(tenant_id), str(broker_account_id), str(symbol))
            )
            self.delete_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated postgres outage on delete")

    backend = _DeleteFailingBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=0.0,
        ),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    # Seed a marker manually and invoke the stale-marker check; the
    # zero max-age forces it through the auto-clear branch.
    from datetime import datetime, timezone
    engine._inflight_markers[("t-1", "A1", "S")] = (
        "BOI-STALE", 0.0, datetime.now(timezone.utc),
    )
    blocked = engine._is_inflight_blocked(
        tenant_id="t-1", broker_account_id="A1", symbol="S",
    )
    assert blocked is False, (
        "stale marker with age > max_age must clear and return False"
    )
    assert backend.delete_raise_on_failure == [True], (
        "stale-marker timeout MUST request raise_on_failure=True in "
        "LIVE so a Postgres delete failure is propagated and surfaced "
        "as an ERROR event (PR #261 round-3 P2 — exit_engines.py:2227)"
    )
    # The in-memory marker is gone even though the durable delete
    # failed — operator visibility comes via the structured ERROR log.
    assert ("t-1", "A1", "S") not in engine._inflight_markers


@pytest.mark.asyncio
async def test_pr261_round3_stale_marker_timeout_paper_keeps_log_only(monkeypatch):
    """In PAPER/SHADOW the stale-marker timeout path must KEEP the
    historical log-only behaviour (``raise_on_failure=False``) so dev
    loops are not destabilised by backend hiccups."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    state_store = StateStore()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3B")
    backend = _FakeInflightBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(
            position_trailing_lock_inflight_max_seconds=0.0,
        ),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    from datetime import datetime, timezone
    engine._inflight_markers[("t-1", "A1", "S")] = (
        "BOI-STALE", 0.0, datetime.now(timezone.utc),
    )
    engine._is_inflight_blocked(
        tenant_id="t-1", broker_account_id="A1", symbol="S",
    )
    assert backend.delete_raise_on_failure == [False], (
        "PAPER/SHADOW must KEEP raise_on_failure=False on the "
        "stale-marker timeout path"
    )


@pytest.mark.asyncio
async def test_pr261_round3_terminal_evidence_clear_surfaces_delete_failure_in_live(
    monkeypatch,
):
    """P2 (exit_engines.py:3072): when the disappeared-symbol sweep
    finds terminal broker evidence in LIVE, the durable
    ``delete_marker`` call MUST opt into ``raise_on_failure=True`` and
    surface failures via the
    ``POSITION_TRAILING_LOCK_INFLIGHT_DELETE_FAILED`` event. Without
    this, a swallowed Postgres delete leaves a stale row that a
    restart would rehydrate."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    from app.brokers.base import OrderStatus
    from datetime import datetime, timezone

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3C")

    class _TerminalDeleteFailingBackend(_FakeInflightBackend):
        def delete_marker(
            self, tenant_id, broker_account_id, symbol,
            *, raise_on_failure: bool = False,
        ):
            self.delete_calls.append(
                (str(tenant_id), str(broker_account_id), str(symbol))
            )
            self.delete_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated postgres outage on terminal-delete")

    backend = _TerminalDeleteFailingBackend()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend()),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers

    # Symbol disappears AND ledger shows the matching exit row in a
    # terminal state — drives the sweep into the terminal-evidence
    # clear path at line ~3068.
    state_store.set_positions("A1", [])
    exit_row = OrderStatus(
        order_id="BOI-261-R3C",
        symbol="NG22MAY26255CE",
        side="SELL",
        status="FILLED",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1250,
        filled_quantity=1250,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    state_store.set_orders("A1", [exit_row])
    state_store.set_order_snapshot("A1", [exit_row])
    _age_inflight_markers_past_grace(engine)
    _mark_positions_sync_fresh(state_store, "A1")
    # Reset the delete-call recording from the pre-arm path so we
    # measure ONLY the terminal-evidence clear-path call.
    backend.delete_raise_on_failure.clear()
    backend.delete_calls.clear()
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # Force a second distinct fingerprint to clear the disappeared-
    # symbol miss-counter threshold.
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert backend.delete_raise_on_failure, (
        "terminal-evidence clear path MUST call delete_marker"
    )
    assert backend.delete_raise_on_failure[-1] is True, (
        "terminal-evidence clear in LIVE MUST pass "
        "raise_on_failure=True (PR #261 round-3 P2 — "
        "exit_engines.py:3072)"
    )


@pytest.mark.asyncio
async def test_pr261_round3_inflight_disabled_still_runs_qty0_cleanup():
    """P2 (exit_engines.py:2584): when ``inflight_disabled=True`` the
    engine MUST still run the qty==0 cleanup paths so a position that
    closes during the backend-outage window does NOT preserve stale
    peak/armed manager state into a same-symbol reopen. Previously the
    top-level ``return`` skipped the entire cycle, including cleanup.
    """
    state_store = StateStore()
    manager = PositionTrailingLockManager(
        backend=_NoopPositionTrailingLockBackend()
    )
    # Pre-seed manager state so we have something to reset.
    manager.evaluate(
        tenant_id="t-1",
        broker_account_id="A1",
        symbol="NG22MAY26255CE",
        current_unrealized_pnl=2500.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert ("t-1", "A1", "NG22MAY26255CE") in manager._states, (
        "test scaffolding: manager must have armed peak"
    )

    # Position now reports qty=0 (closed). Engine runs in
    # inflight_disabled mode — submissions are blocked but the qty==0
    # cleanup path MUST still execute and reset manager state.
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=0, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3D")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
        inflight_disabled=True,
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], (
        "router MUST NOT be invoked when inflight_disabled=True "
        "(submission fail-closed)"
    )
    assert ("t-1", "A1", "NG22MAY26255CE") not in manager._states, (
        "qty==0 cleanup MUST reset manager state even when "
        "inflight_disabled=True so a same-symbol reopen does not "
        "reuse stale peak/armed state (PR #261 round-3 P2 — "
        "exit_engines.py:2584)"
    )


@pytest.mark.asyncio
async def test_pr261_round3_inflight_disabled_still_blocks_submissions():
    """Companion to the cleanup-still-runs test: even with the new
    fall-through evaluator, ``inflight_disabled=True`` must still
    refuse all broker submissions for non-zero positions."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3E")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
        inflight_disabled=True,
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], (
        "inflight_disabled must STILL block submissions for non-zero "
        "positions (preserve fail-closed contract)"
    )


@pytest.mark.asyncio
async def test_pr261_round3_spec_capture_uses_broker_units_not_lots():
    """P2 (exit_engines.py:3283): the marker spec captured at arm time
    MUST record ``exit_plan.broker_units`` (e.g. 1250 for 1 lot of NG)
    rather than ``exit_plan.lots`` (which would be 1). The broker
    ledger row's ``OrderStatus.quantity`` is in broker units after
    the router replaces request quantity with ``broker_qty``, so an
    apples-to-apples comparison requires broker units on both sides.
    """
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3F")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    spec = engine._inflight_exit_specs.get(
        ("t-1", "A1", "NG22MAY26255CE")
    )
    assert spec is not None, "exit spec must be captured at arm time"
    side, units = spec
    assert side == "SELL", "exit side must be SELL for a long NG position"
    assert units == 1250, (
        "spec quantity MUST be broker_units (1250 = 1 lot * 1250 "
        "lot_size), not lots (PR #261 round-3 P2 — "
        "exit_engines.py:3283)"
    )


@pytest.mark.asyncio
async def test_pr261_round3_unknown_id_fallback_matches_broker_units_ledger_row():
    """P2 (exit_engines.py:3283): with the spec captured in broker
    units, an unknown-broker-order-id fallback presented with a real
    matching FILLED row (quantity=1250 broker units) MUST recognise
    it as terminal evidence. Before this fix, the gate compared
    expected_qty=1 (lots) against order.quantity=1250 (units) and the
    marker could only clear by timeout."""
    from app.brokers.base import OrderStatus
    from datetime import datetime, timezone

    class _NoOrderIdRouter:
        def __init__(self):
            self.calls = []

        async def submit_order(
            self, *, tenant_id, broker_account_id, strategy_id, order_req,
        ):
            self.calls.append(order_req)
            return ("hub-order-id", SimpleNamespace(status="OK"))

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _NoOrderIdRouter()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    key = ("t-1", "A1", "NG22MAY26255CE")
    assert key in engine._inflight_markers, "marker must be armed"
    _age_inflight_markers_past_grace(engine)

    # Symbol disappears and the broker ledger shows a matching SELL
    # FILLED row in BROKER UNITS (1250), updated_at after the marker.
    exit_row = OrderStatus(
        order_id="BOI-LATE-EXIT-R3",
        symbol="NG22MAY26255CE",
        side="SELL",
        status="FILLED",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1250,  # broker units, NOT lots
        filled_quantity=1250,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    state_store.set_orders("A1", [exit_row])
    state_store.set_order_snapshot("A1", [exit_row])
    state_store.set_positions("A1", [])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert key not in engine._inflight_markers, (
        "a matching SELL FILLED row with quantity=1250 broker units "
        "MUST clear the marker via the unknown-id fallback (PR #261 "
        "round-3 P2 — exit_engines.py:3283)"
    )


@pytest.mark.asyncio
async def test_pr261_round3_exit_spec_round_trips_via_backend_on_restart():
    """P2 (exit_engines.py:2509): the exit spec MUST be persisted to
    the durable backend alongside the marker and repopulated on
    hydration so the unknown-broker-order-id fallback works after a
    restart. Simulates: arm marker via submit, then construct a fresh
    engine over the same backend, verify ``_inflight_exit_specs``
    survives.
    """
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3G")
    backend = _FakeInflightBackend()
    engine_pre = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine_pre.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine_pre.evaluate_runners([_runner("t-1", "A1")])
    key = ("t-1", "A1", "NG22MAY26255CE")
    pre_spec = engine_pre._inflight_exit_specs.get(key)
    assert pre_spec is not None and pre_spec[0] == "SELL" and pre_spec[1] == 1250, (
        "pre-restart spec must be (SELL, 1250)"
    )

    # Verify the spec was actually written through to the durable
    # backend (the save path includes exit_side / exit_broker_units).
    saved_marker = backend._rows[key]
    assert saved_marker.exit_side == "SELL", (
        "save_marker MUST persist exit_side to the durable backend "
        "(PR #261 round-3 P2 — exit_engines.py:2509)"
    )
    assert saved_marker.exit_broker_units == 1250, (
        "save_marker MUST persist exit_broker_units to the durable "
        "backend (PR #261 round-3 P2 — exit_engines.py:2509)"
    )

    # Construct a FRESH engine instance over the SAME backend to
    # simulate a process restart. Hydration must repopulate the spec.
    engine_post = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    # An evaluate cycle triggers lazy hydration.
    state_store.set_positions("A1", [])  # symbol disappeared post-restart
    _mark_positions_sync_fresh(state_store, "A1")
    await engine_post.evaluate_runners([_runner("t-1", "A1")])
    post_spec = engine_post._inflight_exit_specs.get(key)
    assert post_spec is not None, (
        "post-restart hydration MUST repopulate _inflight_exit_specs "
        "from the durable backend (PR #261 round-3 P2 — "
        "exit_engines.py:2509)"
    )
    assert post_spec[0] == "SELL" and post_spec[1] == 1250, (
        "post-restart spec must round-trip exactly (SELL, 1250)"
    )


@pytest.mark.asyncio
async def test_pr261_round3_legacy_marker_without_spec_hydrates_cleanly():
    """Companion to the round-trip test: rows written before the
    round-3 schema change have NULL ``exit_side``/``exit_broker_units``.
    Hydration MUST tolerate them — leaving ``_inflight_exit_specs``
    empty for that key — so the engine falls back to the broker_order_id
    strict-match path without raising."""
    from app.pnl.position_trailing_lock import (
        PositionTrailingLockInflightMarker,
    )
    from datetime import datetime, timezone

    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R3H")
    backend = _FakeInflightBackend()
    # Seed a "legacy" marker: spec fields default to None.
    backend.save_marker(
        PositionTrailingLockInflightMarker(
            tenant_id="t-1",
            broker_account_id="A1",
            symbol="NG22MAY26255CE",
            broker_order_id="BOI-LEGACY",
            submitted_at=datetime.now(timezone.utc),
        )
    )
    backend.save_calls.clear()
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    # Marker rehydrated.
    assert ("t-1", "A1", "NG22MAY26255CE") in engine._inflight_markers, (
        "legacy marker must still rehydrate to in-memory dict"
    )
    # Spec dict is empty for the legacy key (no NULL injection).
    assert ("t-1", "A1", "NG22MAY26255CE") not in engine._inflight_exit_specs, (
        "legacy markers with NULL spec columns must NOT populate "
        "_inflight_exit_specs — the engine falls back to the "
        "broker_order_id strict path until the spec is repopulated "
        "on the next arm (PR #261 round-3 P2)"
    )
    # Router must be blocked by the rehydrated marker.
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], (
        "rehydrated legacy marker must block duplicate submission"
    )


# ---------------------------------------------------------------------------
# PR #261 round-5 review — three NEW findings (the other 16 are stale-loop
# re-flags of round-3 fixes that are demonstrably in code):
#
#   P2 (migrations/019:29): migration 019 must include exit_side +
#   exit_broker_units columns so fresh deployments where migrations OWN
#   schema get them on the CREATE TABLE — runtime ALTER TABLE on a
#   DML-only app role would silently fail.
#
#   P2 (exit_engines.py:2760): hydrate-pending LIVE gate previously
#   short-circuited the whole evaluator, suppressing qty==0 cleanup. The
#   gate must now block ONLY submissions; cleanup paths still run so a
#   close-during-hydrate-pending cannot preserve stale peak state.
#
#   P2 (exit_engines.py:3274): disappeared-symbol terminal-evidence
#   sweep must NOT reset PositionTrailingLockManager peak/armed state on
#   non-fill terminal statuses (CANCELLED / REJECTED / EXPIRED / FAILED) —
#   those statuses are terminal for the EXIT order but do not prove the
#   position is closed, so the peak the next exit retry needs is lost.
# ---------------------------------------------------------------------------


def test_pr261_round5_migration_019_includes_spec_columns():
    """P2 (migrations/019:29): a fresh deploy that runs migrations
    cleanly (without the runtime ALTER TABLE in the backend ``__init__``
    being able to add the columns — e.g. DML-only app role) must still
    end up with ``exit_side`` and ``exit_broker_units`` columns. Pin
    this in the migration file itself."""
    import os

    migration_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "migrations",
        "019_position_trailing_lock_inflight.sql",
    )
    with open(migration_path, "r", encoding="utf-8") as f:
        sql = f.read()
    upper = sql.upper()
    # The CREATE TABLE must list both columns inline so a DDL-restricted
    # app role doesn't need to run the runtime ALTER.
    assert "EXIT_SIDE" in upper, (
        "migration 019 must declare ``exit_side`` so fresh deploys "
        "have the column without depending on runtime ALTER TABLE "
        "(PR #261 round-5 review P2 — migrations/019:29)"
    )
    assert "EXIT_BROKER_UNITS" in upper, (
        "migration 019 must declare ``exit_broker_units`` so fresh "
        "deploys have the column without depending on runtime ALTER "
        "(PR #261 round-5 review P2 — migrations/019:29)"
    )
    # Idempotent ADD COLUMN IF NOT EXISTS for existing deployments
    # that already ran an earlier revision of this migration.
    assert "ADD COLUMN IF NOT EXISTS EXIT_SIDE" in upper, (
        "migration 019 must include idempotent ADD COLUMN IF NOT "
        "EXISTS for exit_side so existing deployments running the "
        "migration after the round-3 fix do not fail"
    )
    assert "ADD COLUMN IF NOT EXISTS EXIT_BROKER_UNITS" in upper, (
        "migration 019 must include idempotent ADD COLUMN IF NOT "
        "EXISTS for exit_broker_units so existing deployments do not "
        "fail re-applying this migration"
    )


@pytest.mark.asyncio
async def test_pr261_round5_hydrate_pending_preserves_qty0_cleanup(monkeypatch):
    """P2 (exit_engines.py:2760): in LIVE, while the durable inflight
    hydrate has not succeeded (transient Postgres outage on startup),
    the engine MUST still run the qty==0 cleanup so a position that
    closes during the hydrate-pending window does NOT preserve stale
    peak/armed manager state into a same-symbol reopen.

    Previously the hydrate-pending gate ``return``ed at the top of
    ``evaluate_runners``, suppressing the cleanup paths in
    ``_evaluate_runner``. The round-5 fix runs the runner loop with
    ``submissions_blocked=True`` so submissions remain fail-closed but
    cleanup still fires."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    manager = PositionTrailingLockManager(
        backend=_NoopPositionTrailingLockBackend()
    )
    # Pre-seed manager state so we have something to reset.
    manager.evaluate(
        tenant_id="t-1",
        broker_account_id="A1",
        symbol="NG22MAY26255CE",
        current_unrealized_pnl=2500.0,
        floor_inr=500.0,
        giveback_pct=0.10,
        exit_cooldown_seconds=0.0,
    )
    assert ("t-1", "A1", "NG22MAY26255CE") in manager._states, (
        "test scaffolding: manager must have armed peak"
    )

    class _AlwaysFailingHydrateBackend(_FakeInflightBackend):
        def load_all(self, *, raise_on_failure: bool = False):
            self.load_calls += 1
            self.load_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated postgres outage")

    backend = _AlwaysFailingHydrateBackend()
    # Position now reports qty=0 (closed). Engine is in LIVE, durable
    # backend is unreachable so hydrate will never succeed — submissions
    # must be fail-closed BUT qty==0 cleanup must still run.
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=0, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R5A")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=manager,
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert engine._inflight_hydrated is False, (
        "hydrate must NOT have flipped to True with a failing backend"
    )
    assert router.calls == [], (
        "submissions MUST be blocked while hydrate is pending in LIVE "
        "(PR #261 round-2 P1 — exit_engines.py:2405)"
    )
    assert ("t-1", "A1", "NG22MAY26255CE") not in manager._states, (
        "qty==0 cleanup MUST reset manager state even while hydrate "
        "is pending in LIVE so a same-symbol reopen does not reuse "
        "stale peak/armed state (PR #261 round-5 P2 — "
        "exit_engines.py:2760)"
    )


@pytest.mark.asyncio
async def test_pr261_round5_hydrate_pending_still_blocks_submissions(monkeypatch):
    """Companion gate to the round-5 hydrate-pending-cleanup test:
    even with the new cleanup-still-runs evaluator path, a LIVE hydrate-
    pending state MUST still refuse to submit new exits for non-zero
    positions. Otherwise the round-2 P1 fail-closed contract is lost."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )

    class _AlwaysFailingHydrateBackend(_FakeInflightBackend):
        def load_all(self, *, raise_on_failure: bool = False):
            self.load_calls += 1
            self.load_raise_on_failure.append(bool(raise_on_failure))
            raise RuntimeError("simulated postgres outage")

    backend = _AlwaysFailingHydrateBackend()
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R5B")
    engine = PositionTrailingLockEngine(
        settings=_mk_settings(),
        state_store=state_store,
        order_router=router,  # type: ignore[arg-type]
        manager=PositionTrailingLockManager(
            backend=_NoopPositionTrailingLockBackend()
        ),
        inflight_backend=backend,  # type: ignore[arg-type]
    )
    _seed_ltp("NG22MAY26255CE", 16.50)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _seed_ltp("NG22MAY26255CE", 16.27)
    await engine.evaluate_runners([_runner("t-1", "A1")])
    assert router.calls == [], (
        "LIVE hydrate-pending must STILL block submissions for non-zero "
        "positions (preserve PR #261 round-2 P1 fail-closed contract; "
        "round-5 P2 fix expanded but did not relax it)"
    )


@pytest.mark.asyncio
async def test_pr261_round5_cancelled_terminal_evidence_preserves_peak():
    """P2 (exit_engines.py:3274): when the disappeared-symbol sweep
    sees terminal evidence with status=CANCELLED (or any non-FILL
    terminal status), the marker can be cleared but the
    ``PositionTrailingLockManager`` peak/armed state MUST be preserved
    — a CANCELLED exit means the position is likely still open and the
    historic peak the next exit attempt needs has not been invalidated.
    """
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R5C")
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
    assert state_key in manager._states, (
        "test scaffolding: manager state must be armed pre-sweep"
    )
    pre_peak = manager._states[state_key].peak_unrealized_pnl
    assert pre_peak > 0.0, "test scaffolding: peak must be > 0"

    _age_inflight_markers_past_grace(engine)

    # Symbol disappears AND the broker ledger reports the exit order
    # as CANCELLED — terminal for the exit order but the underlying
    # position is still open. Seed via set_orders to exercise the
    # active-projection fallback path; the snapshot path is exercised
    # by other round-1 P2 tests.
    state_store.set_positions("A1", [])
    armed_marker = engine._inflight_markers.get(state_key)
    armed_boi = armed_marker[0] if armed_marker else "BOI-261-R5C"
    _seed_terminal_broker_order(
        state_store, "A1",
        symbol="NG22MAY26255CE", broker_order_id=str(armed_boi),
        status="CANCELLED",
    )
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])  # 1st miss
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])  # 2nd miss

    # Marker should be cleared (CANCELLED IS a canonical terminal
    # state, so the disappeared-symbol terminal-evidence gate fires).
    assert state_key not in engine._inflight_markers, (
        "CANCELLED is a canonical terminal state — the inflight "
        "marker MUST clear so a legitimate same-symbol reopen is "
        "not blocked"
    )
    # Manager peak/armed state MUST NOT be reset — the position is
    # likely still open and the peak must remain valid for the next
    # exit attempt.
    assert state_key in manager._states, (
        "PositionTrailingLockManager peak/armed state MUST be "
        "preserved when terminal evidence is non-fill (CANCELLED). "
        "The position is likely still open; resetting peaks loses "
        "the historic peak the next exit retry needs "
        "(PR #261 round-5 P2 — exit_engines.py:3274)"
    )
    assert manager._states[state_key].peak_unrealized_pnl == pre_peak, (
        "peak value MUST be preserved unchanged on non-fill terminal "
        "evidence"
    )


@pytest.mark.asyncio
async def test_pr261_round5_rejected_terminal_evidence_preserves_peak():
    """Companion to the CANCELLED test — REJECTED is another non-fill
    terminal state and must also preserve manager peak/armed state.
    Pins the OrderLifecycleState.FILLED-only branch so a future refactor
    cannot accidentally widen the reset to all terminal states."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R5D")
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
    assert state_key in manager._states
    _age_inflight_markers_past_grace(engine)

    state_store.set_positions("A1", [])
    armed_marker = engine._inflight_markers.get(state_key)
    armed_boi = armed_marker[0] if armed_marker else "BOI-261-R5D"
    _seed_terminal_broker_order(
        state_store, "A1",
        symbol="NG22MAY26255CE", broker_order_id=str(armed_boi),
        status="REJECTED",
    )
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert state_key not in engine._inflight_markers, (
        "REJECTED clears the marker (terminal for the exit order)"
    )
    assert state_key in manager._states, (
        "PositionTrailingLockManager peak/armed state MUST be "
        "preserved on REJECTED terminal evidence — the position is "
        "still open (PR #261 round-5 P2 — exit_engines.py:3274)"
    )


@pytest.mark.asyncio
async def test_pr261_round5_filled_terminal_evidence_still_resets_peak():
    """Counterpart to the non-fill tests — FILLED MUST still reset
    the manager state. Without this, the new round-5 branching could
    accidentally suppress the legitimate FILLED reset path and a quick
    same-symbol reopen would reuse the prior peak (the original
    issue #250 scenario)."""
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [Position(symbol="NG22MAY26255CE", quantity=1250, avg_price=14.30,
                  product_type=ProductType.INTRADAY)],
    )
    manager = PositionTrailingLockManager(backend=_NoopPositionTrailingLockBackend())
    router = _RouterReturningBrokerOrderId(broker_order_id="BOI-261-R5E")
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
    assert state_key in manager._states
    _age_inflight_markers_past_grace(engine)

    state_store.set_positions("A1", [])
    armed_marker = engine._inflight_markers.get(state_key)
    armed_boi = armed_marker[0] if armed_marker else "BOI-261-R5E"
    _seed_terminal_broker_order(
        state_store, "A1",
        symbol="NG22MAY26255CE", broker_order_id=str(armed_boi),
        status="FILLED",
    )
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])
    _mark_positions_sync_fresh(state_store, "A1")
    await engine.evaluate_runners([_runner("t-1", "A1")])

    assert state_key not in engine._inflight_markers
    assert state_key not in manager._states, (
        "FILLED MUST reset manager state — the position is now flat "
        "and the prior peak is no longer relevant (issue #250)"
    )
