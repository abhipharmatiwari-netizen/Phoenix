"""Tests for the /admin/kill-switch/cancel-all endpoint and the
LIVE-mode fail-closed behaviour of ``_save_kill_switch_state``
(issue #238).

The endpoint walks every registered account runner and asks the
broker adapter to cancel each non-terminal order it knows about. The
function is intentionally idempotent — a cancel against an already-
cancelled / unknown order surfaces as a REJECTED ``OrderResponse``
which the endpoint records under ``skipped`` rather than failing the
batch.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from fastapi import HTTPException

from app.brokers.base import OrderResponse
from app.dashboard import admin_routes
from app.dashboard.admin_routes import (
    KillSwitchCancelAllRequest,
    _save_kill_switch_state,
    kill_switch_cancel_all,
)
from app.dashboard.auth import AdminContext, AdminRole


# ---- helpers -----------------------------------------------------------


def _mk_admin(role: AdminRole = AdminRole.ADMIN) -> AdminContext:
    return AdminContext(caller="admin@phoenix.com", role=role)


class _FakeBroker:
    """Records cancel_order + get_orders calls and returns configurable
    responses. PR #240 round-1 review: cancel-all now refreshes
    broker orders before iterating, so the broker stub must also
    implement ``get_orders``."""

    def __init__(
        self,
        *,
        responses: Optional[List[OrderResponse]] = None,
        raise_for: Optional[set[str]] = None,
        get_orders_result: Optional[List[Any]] = None,
        get_orders_raises: bool = False,
    ) -> None:
        self.calls: List[dict] = []
        self.get_orders_calls = 0
        self._responses: List[OrderResponse] = list(responses or [])
        self._raise_for = set(raise_for or [])
        self._get_orders_result = get_orders_result
        self._get_orders_raises = get_orders_raises

    async def get_orders(self) -> List[Any]:
        self.get_orders_calls += 1
        if self._get_orders_raises:
            raise RuntimeError("simulated get_orders outage")
        if self._get_orders_result is not None:
            return list(self._get_orders_result)
        return []  # default; caller falls back to ``_last_orders``

    async def cancel_order(
        self,
        order_id: str,
        *,
        symbol: Optional[str] = None,
        variety: Optional[str] = None,
    ) -> OrderResponse:
        self.calls.append({
            "order_id": order_id,
            "symbol": symbol,
            "variety": variety,
        })
        if order_id in self._raise_for:
            raise RuntimeError(f"simulated broker outage for {order_id}")
        if self._responses:
            return self._responses.pop(0)
        # Default: report cancelled.
        return OrderResponse(
            broker_order_id=order_id,
            status="CANCELLED",
            message="cancelled",
            filled_quantity=0,
            average_price=None,
        )


def _mk_runner(broker_account_id: str, orders: List[Any], broker: _FakeBroker):
    runner = SimpleNamespace(
        broker_account_id=broker_account_id,
        is_running=True,
    )
    runner._broker_client = broker
    runner._last_orders = orders
    # PR #240 round-1 review P1: the endpoint refreshes broker orders
    # via ``get_orders()`` before iterating, so the fake broker must
    # surface the SAME list there unless a test explicitly wants the
    # refreshed list to differ from the cache.
    if broker._get_orders_result is None:
        broker._get_orders_result = list(orders)
    return runner


def _mk_runtime(runners: dict):
    hub = SimpleNamespace(
        list_runner_ids=lambda: list(runners.keys()),
        get_runner=lambda acct_id: runners.get(acct_id),
    )
    return SimpleNamespace(hub=hub)


def _order(
    order_id: str,
    status: str = "OPEN",
    symbol: str = "X",
    variety: str = "NORMAL",
) -> Any:
    """Build a stub ``OrderStatus``-shaped object. Round-1 review P1:
    the real field name is ``order_id`` not ``broker_order_id``."""
    return SimpleNamespace(
        order_id=order_id,
        status=status,
        symbol=symbol,
        variety=variety,
    )


def _run(coro):
    return asyncio.run(coro)


# ---- auth ---------------------------------------------------------------


def _runner_with_get_orders_result(broker_account_id, broker, get_orders_orders):
    """Make a runner whose broker.get_orders() returns the given orders.
    Used to verify the cancel-all endpoint refreshes from broker rather
    than reading the cached ``_last_orders``."""
    runner = SimpleNamespace(
        broker_account_id=broker_account_id,
        is_running=True,
    )
    runner._broker_client = broker
    runner._last_orders = []  # intentionally empty — cache is stale
    broker._get_orders_result = get_orders_orders
    return runner


def test_cancel_all_requires_admin_role(monkeypatch):
    """OPERATOR is not enough — cancel-all needs ADMIN."""
    runtime = _mk_runtime({})
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: runtime)
    with pytest.raises(HTTPException) as exc:
        _run(
            kill_switch_cancel_all(
                KillSwitchCancelAllRequest(reason="x"),
                _mk_admin(AdminRole.OPERATOR),
            )
        )
    assert exc.value.status_code == 403


def test_cancel_all_requires_non_empty_reason(monkeypatch):
    runtime = _mk_runtime({})
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: runtime)
    with pytest.raises(HTTPException) as exc:
        _run(
            kill_switch_cancel_all(
                KillSwitchCancelAllRequest(reason="   "),
                _mk_admin(),
            )
        )
    assert exc.value.status_code == 422
    assert "reason" in str(exc.value.detail).lower()


# ---- happy path --------------------------------------------------------


def test_cancel_all_cancels_each_open_order_across_runners(monkeypatch):
    broker_a = _FakeBroker()
    broker_b = _FakeBroker()
    runners = {
        "A1": _mk_runner("A1", [_order("o1"), _order("o2")], broker_a),
        "A2": _mk_runner("A2", [_order("o3")], broker_b),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    captured: List[dict] = []
    monkeypatch.setattr(
        admin_routes, "emit_audit_event",
        lambda **kw: captured.append(kw),
    )
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="panic stop"),
        _mk_admin(),
    ))
    assert resp["status"] == "ok"
    assert resp["attempted"] == 3
    assert resp["cancelled"] == 3
    assert resp["failed"] == 0
    assert len(broker_a.calls) == 2
    assert len(broker_b.calls) == 1
    # Audit event recorded with reason + per-account summary.
    assert captured, "audit event must be emitted"
    audit = captured[-1]
    assert audit["action"] == "kill_switch_cancel_all"
    assert audit["metadata"]["reason"] == "panic stop"
    assert audit["metadata"]["attempted"] == 3
    assert audit["metadata"]["cancelled"] == 3


def test_cancel_all_skips_terminal_orders(monkeypatch):
    """Already-FILLED / CANCELLED orders are counted as skipped, not
    attempted — broker never sees a no-op cancel."""
    broker = _FakeBroker()
    runners = {
        "A1": _mk_runner("A1", [
            _order("o1", status="FILLED"),
            _order("o2", status="CANCELLED"),
            _order("o3", status="OPEN"),
        ], broker),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="drain"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    assert resp["skipped"] == 2
    assert len(broker.calls) == 1
    assert broker.calls[0]["order_id"] == "o3"


def test_cancel_all_idempotent_on_rejected_response(monkeypatch):
    """A broker REJECTED response (e.g. order already cancelled or
    unknown) is recorded as ``skipped`` so an operator double-click
    does not surface as a hard failure."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="REJECTED",
            message="order_not_found",
            filled_quantity=0,
            average_price=None,
        ),
    ])
    runners = {
        "A1": _mk_runner("A1", [_order("o1")], broker),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="retry"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 0
    assert resp["failed"] == 0
    assert resp["skipped"] == 1
    assert resp["status"] == "ok"  # not partial — idempotent


def test_cancel_all_records_broker_exception_as_failed(monkeypatch):
    """Genuine broker outage (raised exception) surfaces in the
    per-account errors list and bumps ``failed``."""
    broker = _FakeBroker(raise_for={"o1"})
    runners = {
        "A1": _mk_runner("A1", [_order("o1"), _order("o2")], broker),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="emergency"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 2
    assert resp["cancelled"] == 1
    assert resp["failed"] == 1
    assert resp["status"] == "partial"
    per_a1 = next(r for r in resp["per_account"] if r["broker_account_id"] == "A1")
    assert per_a1["status"] == "partial"
    assert per_a1["errors"], "broker outage must populate errors[]"
    assert "simulated broker outage" in str(per_a1["errors"][0]["error"])


def test_cancel_all_404_when_specific_broker_account_unknown(monkeypatch):
    runners = {"A1": _mk_runner("A1", [], _FakeBroker())}
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x", broker_account_id="UNKNOWN"),
            _mk_admin(),
        ))
    assert exc.value.status_code == 404


def test_cancel_all_scopes_to_specific_broker_account(monkeypatch):
    broker_a = _FakeBroker()
    broker_b = _FakeBroker()
    runners = {
        "A1": _mk_runner("A1", [_order("a1-o1")], broker_a),
        "A2": _mk_runner("A2", [_order("a2-o1")], broker_b),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="targeted", broker_account_id="A1"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert len(broker_a.calls) == 1
    assert len(broker_b.calls) == 0
    assert len(resp["per_account"]) == 1
    assert resp["per_account"][0]["broker_account_id"] == "A1"


def test_cancel_all_skips_runner_with_no_cancel_api(monkeypatch):
    runner = SimpleNamespace(
        broker_account_id="A_NOAPI",
        is_running=True,
    )
    runner._broker_client = SimpleNamespace()  # no cancel_order attribute
    runner._last_orders = [_order("o1")]
    runners = {"A_NOAPI": runner}
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="check_noapi"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 0
    per = resp["per_account"][0]
    assert per["status"] == "broker_no_cancel_api"


# ---- LIVE fail-closed save_state ---------------------------------------


def test_save_kill_switch_state_non_live_swallows_postgres_failure(monkeypatch):
    """Non-LIVE: a Postgres outage logs a warning and returns
    silently (existing behaviour preserved for local/dev/test)."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")

    class _BoomConnect:
        def __enter__(self): raise RuntimeError("postgres unreachable")
        def __exit__(self, *_a): pass

    import app.data.postgres as _pg
    monkeypatch.setattr(_pg, "connect_with_retry", lambda *_a, **_kw: _BoomConnect())
    monkeypatch.setattr(_pg, "get_control_plane_dsn", lambda *_a, **_kw: "postgresql://x")
    fake_ksm = SimpleNamespace(save_state=lambda _conn: None)
    _save_kill_switch_state(fake_ksm)  # must NOT raise


def test_save_kill_switch_state_live_fails_closed_with_500(monkeypatch):
    """LIVE: a Postgres outage during persist raises HTTP 500 so the
    operator does not see a phantom TRIPPED state that would vanish on
    restart."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")

    class _BoomConnect:
        def __enter__(self): raise RuntimeError("postgres unreachable")
        def __exit__(self, *_a): pass

    import app.data.postgres as _pg
    monkeypatch.setattr(_pg, "connect_with_retry", lambda *_a, **_kw: _BoomConnect())
    monkeypatch.setattr(_pg, "get_control_plane_dsn", lambda *_a, **_kw: "postgresql://x")
    fake_ksm = SimpleNamespace(save_state=lambda _conn: None)
    with pytest.raises(HTTPException) as exc:
        _save_kill_switch_state(fake_ksm)
    assert exc.value.status_code == 500
    assert "durable" in str(exc.value.detail).lower() or "persist" in str(exc.value.detail).lower()


# ---- PR #240 round-1 review additions ----------------------------------


def test_cancel_all_refreshes_broker_orders_before_iterating(monkeypatch):
    """Round-1 P1: cancel-all must call ``broker.get_orders()`` so it
    does not work from a 90s-stale ``_last_orders`` cache. A fresh
    open order returned only by the broker refresh must be cancelled."""
    broker = _FakeBroker()
    runner = _runner_with_get_orders_result(
        "A1",
        broker,
        get_orders_orders=[_order("fresh-o1")],
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="fresh"),
        _mk_admin(),
    ))
    assert broker.get_orders_calls == 1
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    assert broker.calls[0]["order_id"] == "fresh-o1"


def test_cancel_all_falls_back_to_cached_orders_on_refresh_failure(monkeypatch):
    """If ``broker.get_orders()`` raises, the endpoint must still
    attempt cancellation against the cached ``_last_orders`` so a
    broker outage doesn't silently make cancel-all a no-op."""
    broker = _FakeBroker(get_orders_raises=True)
    runner = _mk_runner("A1", [_order("cached-o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="fallback"),
        _mk_admin(),
    ))
    assert broker.get_orders_calls == 1
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    # The refresh error is surfaced in per_account.errors[].
    per = resp["per_account"][0]
    assert any("broker_orders_refresh_error" in str(e) for e in per["errors"])


def test_cancel_all_passes_order_variety_to_broker(monkeypatch):
    """Round-1 P2: AMO / STOPLOSS orders carry a non-default
    ``variety`` that must be forwarded to ``cancel_order``."""
    broker = _FakeBroker()
    runner = _mk_runner(
        "A1",
        [
            _order("amo1", variety="AMO"),
            _order("sl1", variety="STOPLOSS"),
            _order("n1", variety="NORMAL"),
        ],
        broker,
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="variety"),
        _mk_admin(),
    ))
    varieties_sent = [c["variety"] for c in broker.calls]
    assert varieties_sent == ["AMO", "STOPLOSS", "NORMAL"]


def test_cancel_all_error_status_counts_as_failed_not_skipped(monkeypatch):
    """Round-1 P1: real Angel adapter returns ``status=ERROR`` for
    genuine cancel failures (``cancel_failed:...``). MUST count as
    failed so the dashboard shows ``failed>0`` during an incident."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="ERROR",
            message="cancel_failed:broker_outage",
            filled_quantity=0,
            average_price=None,
        ),
    ])
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="incident"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["failed"] == 1
    assert resp["cancelled"] == 0
    assert resp["skipped"] == 0
    assert resp["status"] == "partial"


def test_cancel_all_filled_race_counts_as_raced_filled_not_cancelled(monkeypatch):
    """Round-1 P2: if the broker races the cancel with a FILL, that
    is NEW exposure — the operator may need to flatten. Must NOT be
    counted as ``cancelled``."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="FILLED",
            message="filled during cancel",
            filled_quantity=10,
            average_price=12.5,
        ),
    ])
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="race"),
        _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 0
    assert resp["raced_filled"] == 1
    assert resp["failed"] == 0
    assert resp["status"] == "partial"
    per = resp["per_account"][0]
    assert per["raced_filled"] == 1


def test_cancel_all_rejected_status_remains_idempotent_skip(monkeypatch):
    """REJECTED (e.g. cancel of already-cancelled order) is still
    idempotent — operator double-click must not surface as failed."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="REJECTED",
            message="order_not_found",
            filled_quantity=0,
            average_price=None,
        ),
    ])
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="retry"),
        _mk_admin(),
    ))
    assert resp["failed"] == 0
    assert resp["skipped"] == 1
    assert resp["status"] == "ok"


def test_cancel_all_rejects_out_of_scope_broker_account(monkeypatch):
    """Round-1 P1: a scoped admin (bearer-auth, broker_account_ids
    restricted) must not be allowed to cancel orders for an account
    outside their entitlements, even by passing the id explicitly."""
    broker = _FakeBroker()
    runners = {"OTHER_ACCT": _mk_runner("OTHER_ACCT", [_order("o1")], broker)}
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    scoped_admin = AdminContext(
        caller="scoped@phoenix.com",
        role=AdminRole.ADMIN,
        broker_account_ids=("ALLOWED_ACCT",),
        all_tenants=False,
    )
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x", broker_account_id="OTHER_ACCT"),
            scoped_admin,
        ))
    assert exc.value.status_code == 403


def test_cancel_all_filters_runners_to_scoped_accounts(monkeypatch):
    """When ``broker_account_id`` is omitted, a scoped admin must
    only see runners within their entitlement."""
    broker_a = _FakeBroker()
    broker_b = _FakeBroker()
    runners = {
        "ALLOWED": _mk_runner("ALLOWED", [_order("a1")], broker_a),
        "BLOCKED": _mk_runner("BLOCKED", [_order("b1")], broker_b),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    scoped_admin = AdminContext(
        caller="scoped@phoenix.com",
        role=AdminRole.ADMIN,
        broker_account_ids=("ALLOWED",),
        all_tenants=False,
    )
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="x"),
        scoped_admin,
    ))
    assert len(resp["per_account"]) == 1
    assert resp["per_account"][0]["broker_account_id"] == "ALLOWED"
    assert len(broker_a.calls) == 1
    assert len(broker_b.calls) == 0


def test_save_kill_switch_state_live_invokes_rollback_before_500(monkeypatch):
    """Round-1 P1: when LIVE persist fails, the optional rollback
    callable must fire BEFORE the HTTPException so the in-memory
    state is restored. Otherwise a failed rearm would leave the
    process INACTIVE and allow orders despite the dashboard error."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")

    class _BoomConnect:
        def __enter__(self): raise RuntimeError("postgres unreachable")
        def __exit__(self, *_a): pass

    import app.data.postgres as _pg
    monkeypatch.setattr(_pg, "connect_with_retry", lambda *_a, **_kw: _BoomConnect())
    monkeypatch.setattr(_pg, "get_control_plane_dsn", lambda *_a, **_kw: "postgresql://x")
    fake_ksm = SimpleNamespace(save_state=lambda _conn: None)
    rolled_back: List[bool] = []
    with pytest.raises(HTTPException) as exc:
        _save_kill_switch_state(
            fake_ksm,
            rollback=lambda: rolled_back.append(True),
        )
    assert exc.value.status_code == 500
    assert rolled_back == [True]
