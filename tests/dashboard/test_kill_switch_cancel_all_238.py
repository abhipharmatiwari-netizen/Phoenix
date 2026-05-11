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


def _mk_request() -> Any:
    """Stand-in for FastAPI ``Request`` — used by ``check_rate_limit``."""
    return SimpleNamespace(
        headers={"X-Request-Id": "req-test-1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _patch_cancel_all_preconditions(monkeypatch):
    """Bypass ``check_rate_limit`` and stub the durable kill switch
    as TRIPPED so the new server-side trip-before-cancel gate
    (PR #240 round-3 review P2) does not block tests."""
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda _request: None,
    )
    from app.risk.kill_switch import KillSwitchScope, KillSwitchState
    fake_record = SimpleNamespace(
        scope=KillSwitchScope.GLOBAL,
        scope_id="GLOBAL",
        state=KillSwitchState.TRIPPED,
        block_exits=False,
    )
    fake_ksm = SimpleNamespace(
        get_record=lambda scope, scope_id: (
            fake_record if scope == KillSwitchScope.GLOBAL else None
        ),
    )
    monkeypatch.setattr(
        admin_routes, "_get_kill_switch_manager",
        lambda: fake_ksm,
    )


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
                _mk_request(),
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
                _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="panic stop"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="drain"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="retry"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="emergency"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x", broker_account_id="UNKNOWN"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="targeted", broker_account_id="A1"),
            _mk_request(),
            _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert len(broker_a.calls) == 1
    assert len(broker_b.calls) == 0
    assert len(resp["per_account"]) == 1
    assert resp["per_account"][0]["broker_account_id"] == "A1"


def test_cancel_all_no_cancel_api_runner_is_failed_partial(monkeypatch):
    """PR #240 round-2 review P2: a runner without a callable
    cancel API has NOT been drained — it must contribute to the
    failed count + partial overall status so the dashboard does
    not show a green ok cancel-all while one account could still
    have open broker orders."""
    runner = SimpleNamespace(
        broker_account_id="A_NOAPI",
        is_running=True,
    )
    runner._broker_client = SimpleNamespace()  # no cancel_order attribute
    runner._last_orders = [_order("o1")]
    runners = {"A_NOAPI": runner}
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="check_noapi"),
            _mk_request(),
            _mk_admin(),
    ))
    assert resp["attempted"] == 0
    assert resp["failed"] == 1
    assert resp["status"] == "partial"
    per = resp["per_account"][0]
    assert per["status"] == "broker_no_cancel_api"
    assert per["failed"] == 1


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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="fresh"),
            _mk_request(),
            _mk_admin(),
    ))
    assert broker.get_orders_calls == 1
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    assert broker.calls[0]["order_id"] == "fresh-o1"


def test_cancel_all_falls_back_to_cached_orders_on_refresh_failure(monkeypatch):
    """If ``broker.get_orders()`` raises, the endpoint must still
    attempt cancellation against the cached ``_last_orders`` so a
    broker outage doesn't silently make cancel-all a no-op.

    PR #240 round-2 review P2: when the refresh fails we can NOT
    verify the broker's current open-order set, so per_account
    status must be ``partial`` and the aggregate ``partial`` /
    ``refresh_failures>0`` rather than green ok."""
    broker = _FakeBroker(get_orders_raises=True)
    runner = _mk_runner("A1", [_order("cached-o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="fallback"),
            _mk_request(),
            _mk_admin(),
    ))
    assert broker.get_orders_calls == 1
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    # PR #240 round-2 review P2: refresh failure escalates to partial.
    assert resp["status"] == "partial"
    assert resp["refresh_failures"] == 1
    per = resp["per_account"][0]
    assert per["status"] == "partial"
    assert per["broker_orders_refresh_failed"] is True
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
    _patch_cancel_all_preconditions(monkeypatch)
    _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="variety"),
            _mk_request(),
            _mk_admin(),
    ))
    varieties_sent = [c["variety"] for c in broker.calls]
    assert varieties_sent == ["AMO", "STOPLOSS", "NORMAL"]


def test_cancel_all_error_status_counts_as_failed_not_skipped(monkeypatch):
    """Round-1 P1 + round-4 P2: real Angel adapter returns
    ``status=ERROR`` for genuine cancel failures. MUST count as
    failed so the dashboard shows ``failed>0`` during an incident.
    Round-4 P2 added a retry loop with up to 3 attempts on
    ERROR/TIMEOUT/UNKNOWN responses, so this test queues an ERROR
    response for every attempt to verify the final classification."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="ERROR",
            message="cancel_failed:broker_outage",
            filled_quantity=0,
            average_price=None,
        ),
        OrderResponse(
            broker_order_id="o1",
            status="ERROR",
            message="cancel_failed:broker_outage",
            filled_quantity=0,
            average_price=None,
        ),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="incident"),
            _mk_request(),
            _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["failed"] == 1
    assert resp["cancelled"] == 0
    assert resp["skipped"] == 0
    assert resp["status"] == "partial"
    # Retry loop should have exercised all 3 attempts.
    assert len(broker.calls) == 3


def test_cancel_all_retry_succeeds_after_transient_error(monkeypatch):
    """Round-4 P2: a single transient ERROR followed by a successful
    cancel should report ``cancelled=1, failed=0`` thanks to the
    retry loop, instead of failing on the first blip."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="ERROR",
            message="transient_glitch",
            filled_quantity=0,
            average_price=None,
        ),
        OrderResponse(
            broker_order_id="o1",
            status="CANCELLED",
            message="cancelled",
            filled_quantity=0,
            average_price=None,
        ),
    ])
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="retry-success"),
        _mk_request(),
        _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    assert resp["failed"] == 0
    assert resp["status"] == "ok"
    # Two calls: first ERROR, retry CANCELLED.
    assert len(broker.calls) == 2


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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="race"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="retry"),
            _mk_request(),
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
    _patch_cancel_all_preconditions(monkeypatch)
    scoped_admin = AdminContext(
        caller="scoped@phoenix.com",
        role=AdminRole.ADMIN,
        broker_account_ids=("ALLOWED_ACCT",),
        all_tenants=False,
    )
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x", broker_account_id="OTHER_ACCT"),
            _mk_request(),
            scoped_admin,
        ))
    assert exc.value.status_code == 403


def test_cancel_all_filters_runners_to_scoped_accounts(monkeypatch):
    """When ``broker_account_id`` is omitted, a scoped admin drains
    only runners within their entitlement, AND the response reports
    out-of-scope accounts as ``out_of_scope`` partial entries so the
    operator does not assume every runner was drained.

    PR #240 round-5 review P2: previously the scope filter was
    silent; that hid undrained accounts behind a green ok.
    """
    broker_a = _FakeBroker()
    broker_b = _FakeBroker()
    runners = {
        "ALLOWED": _mk_runner("ALLOWED", [_order("a1")], broker_a),
        "BLOCKED": _mk_runner("BLOCKED", [_order("b1")], broker_b),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    scoped_admin = AdminContext(
        caller="scoped@phoenix.com",
        role=AdminRole.ADMIN,
        broker_account_ids=("ALLOWED",),
        all_tenants=False,
    )
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x"),
            _mk_request(),
            scoped_admin,
    ))
    # Both runners are reported: ALLOWED drained, BLOCKED skipped.
    assert len(resp["per_account"]) == 2
    by_id = {r["broker_account_id"]: r for r in resp["per_account"]}
    assert by_id["ALLOWED"]["status"] == "ok"
    assert by_id["ALLOWED"]["cancelled"] == 1
    assert by_id["BLOCKED"]["status"] == "out_of_scope"
    assert by_id["BLOCKED"]["attempted"] == 0
    # Aggregate verdict reflects the out-of-scope accounts.
    assert resp["out_of_scope"] == 1
    assert resp["status"] == "partial"
    # Broker calls confirm only ALLOWED was touched.
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


# ---- PR #240 round-2 review additions ----------------------------------


def test_cancel_all_rejected_cancel_failed_counts_as_failed(monkeypatch):
    """Round-2 P1: Angel returns ``status=REJECTED`` with message
    ``cancel_failed`` for genuine broker-level cancel rejections too.
    Only messages indicating the order is already gone / cancelled
    / terminal should be treated as idempotent skip; everything else
    must count as failed so the dashboard does not report a green
    cancel-all while orders remain live."""
    broker = _FakeBroker(responses=[
        OrderResponse(
            broker_order_id="o1",
            status="REJECTED",
            message="cancel_failed:broker_server_error",
            filled_quantity=0,
            average_price=None,
        ),
    ])
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="incident"),
            _mk_request(),
            _mk_admin(),
    ))
    assert resp["attempted"] == 1
    assert resp["failed"] == 1
    assert resp["skipped"] == 0
    assert resp["status"] == "partial"
    per = resp["per_account"][0]
    assert per["failed"] == 1


def test_cancel_all_rejected_already_cancelled_counts_as_skipped(monkeypatch):
    """Round-2 P1: message-based disambiguation. Variants of
    "already cancelled" / "not found" remain idempotent skips."""
    for msg in (
        "Order already cancelled",
        "ORDER NOT FOUND",
        "order does not exist",
        "already_filled",
        "Terminal state - cannot cancel",
    ):
        broker = _FakeBroker(responses=[
            OrderResponse(
                broker_order_id="o1",
                status="REJECTED",
                message=msg,
                filled_quantity=0,
                average_price=None,
            ),
        ])
        runner = _mk_runner("A1", [_order("o1")], broker)
        monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
        monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
        _patch_cancel_all_preconditions(monkeypatch)
        resp = _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="retry"),
            _mk_request(),
            _mk_admin(),
        ))
        assert resp["failed"] == 0, f"message {msg!r} should be idempotent skip"
        assert resp["skipped"] == 1, f"message {msg!r} should be idempotent skip"
        assert resp["status"] == "ok", f"message {msg!r} should not be partial"


def test_cancel_all_endpoint_requires_admin_not_operator(monkeypatch):
    """Round-2 P2: cancel-all already required ADMIN; this is a
    regression guard that it has not been loosened back to OPERATOR."""
    runner = _mk_runner("A1", [_order("o1")], _FakeBroker())
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x"),
            _mk_request(),
            _mk_admin(AdminRole.OPERATOR),
        ))
    assert exc.value.status_code == 403


def test_kill_switch_trip_requires_admin_role(monkeypatch):
    """Round-2 P2: trip endpoint now requires ADMIN (was OPERATOR).
    Issue #238 acceptance: non-admin users cannot call the toggle API."""
    from app.dashboard.admin_routes import (
        KillSwitchTripRequest, kill_switch_trip,
    )
    fake_ksm = SimpleNamespace(
        trip=lambda *a, **kw: SimpleNamespace(id="rec", state=SimpleNamespace(value="TRIPPED"), block_exits=False),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: SimpleNamespace(kill_switch_manager=fake_ksm))
    with pytest.raises(HTTPException) as exc:
        kill_switch_trip(
            KillSwitchTripRequest(scope="GLOBAL", scope_id="GLOBAL", reason="x"),
            _mk_admin(AdminRole.OPERATOR),
        )
    assert exc.value.status_code == 403


def test_kill_switch_rearm_requires_admin_role(monkeypatch):
    """Round-2 P2: rearm endpoint now requires ADMIN."""
    from app.dashboard.admin_routes import (
        KillSwitchRearmRequest, kill_switch_rearm,
    )
    fake_ksm = SimpleNamespace(
        rearm=lambda *a, **kw: SimpleNamespace(id="rec", state=SimpleNamespace(value="INACTIVE")),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: SimpleNamespace(kill_switch_manager=fake_ksm))
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    with pytest.raises(HTTPException) as exc:
        kill_switch_rearm(
            KillSwitchRearmRequest(scope="GLOBAL", scope_id="GLOBAL"),
            _mk_admin(AdminRole.OPERATOR),
        )
    assert exc.value.status_code == 403


def test_kill_switch_confirm_clear_requires_step_up_token_in_live(monkeypatch):
    """Round-2 P1: confirm-clear in LIVE mode must require a step-up
    token — that's the transition that restores entry eligibility."""
    from app.dashboard.admin_routes import (
        KillSwitchRearmRequest, kill_switch_confirm_clear,
    )
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    with pytest.raises(HTTPException) as exc:
        kill_switch_confirm_clear(
            KillSwitchRearmRequest(scope="GLOBAL", scope_id="GLOBAL"),
            _mk_admin(),
        )
    assert exc.value.status_code == 403
    assert "step_up_token" in str(exc.value.detail).lower()


def test_kill_switch_confirm_clear_passes_through_in_non_live(monkeypatch):
    """In non-LIVE no step-up token is required."""
    from app.dashboard.admin_routes import (
        KillSwitchRearmRequest, kill_switch_confirm_clear,
    )
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    fake_record = SimpleNamespace(id="rec", state=SimpleNamespace(value="CLEARED"))
    fake_ksm = SimpleNamespace(
        confirm_clear=lambda scope, scope_id: fake_record,
        _key=lambda scope, scope_id: (str(scope), str(scope_id)),
        _records={},
        _lock=__import__("threading").RLock(),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: SimpleNamespace(kill_switch_manager=fake_ksm))
    monkeypatch.setattr(admin_routes, "_save_kill_switch_state", lambda *_a, **_kw: None)
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    # NB: this test does NOT call cancel-all so the precondition
    # patch is intentionally omitted; confirm_clear has its own
    # kill-switch manager fixture above.
    resp = kill_switch_confirm_clear(
        KillSwitchRearmRequest(scope="GLOBAL", scope_id="GLOBAL"),
        _mk_admin(),
    )
    assert resp["status"] == "cleared"


# ---- PR #240 round-3 review additions ----------------------------------


def test_cancel_all_rejected_when_kill_switch_inactive(monkeypatch):
    """Round-3 P2: cancel-all must reject when GLOBAL durable
    kill switch is INACTIVE / CLEARED. Otherwise Phoenix is free to
    place fresh orders during the bulk-cancel window."""
    runner = _mk_runner("A1", [_order("o1")], _FakeBroker())
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda _request: None,
    )
    # Stub KSM with INACTIVE GLOBAL state (no record).
    fake_ksm = SimpleNamespace(get_record=lambda *_a: None)
    monkeypatch.setattr(admin_routes, "_get_kill_switch_manager", lambda: fake_ksm)
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x"),
            _mk_request(),
            _mk_admin(),
        ))
    assert exc.value.status_code == 409
    assert "trip" in str(exc.value.detail).lower()


def test_cancel_all_rejected_when_kill_switch_cleared(monkeypatch):
    """Round-3 P2: CLEARED state also lets new placements through, so
    cancel-all must reject in that state too."""
    runner = _mk_runner("A1", [_order("o1")], _FakeBroker())
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda _request: None,
    )
    from app.risk.kill_switch import KillSwitchScope, KillSwitchState
    fake_record = SimpleNamespace(
        scope=KillSwitchScope.GLOBAL,
        scope_id="GLOBAL",
        state=KillSwitchState.CLEARED,
        block_exits=False,
    )
    fake_ksm = SimpleNamespace(get_record=lambda *_a: fake_record)
    monkeypatch.setattr(admin_routes, "_get_kill_switch_manager", lambda: fake_ksm)
    with pytest.raises(HTTPException) as exc:
        _run(kill_switch_cancel_all(
            KillSwitchCancelAllRequest(reason="x"),
            _mk_request(),
            _mk_admin(),
        ))
    assert exc.value.status_code == 409


def test_cancel_all_rate_limit_enforced(monkeypatch):
    """Round-3 P2: cancel-all calls ``check_rate_limit(request)``
    like every other state-changing admin endpoint."""
    runner = _mk_runner("A1", [_order("o1")], _FakeBroker())
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    rate_limit_calls: List[Any] = []
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda request: rate_limit_calls.append(request),
    )
    _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="rl"),
        _mk_request(),
        _mk_admin(),
    ))
    assert len(rate_limit_calls) == 1, (
        "cancel-all must invoke check_rate_limit on the request"
    )


def test_cancel_all_disappeared_runner_counted_as_failed(monkeypatch):
    """Round-3 P2: list_runner_ids returns id but get_runner returns
    None — surface as failed=1 rather than ok with no_runner+failed=0."""
    hub = SimpleNamespace(
        list_runner_ids=lambda: ["A_GONE"],
        get_runner=lambda _: None,  # disappeared race
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: SimpleNamespace(hub=hub))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="race"),
        _mk_request(),
        _mk_admin(),
    ))
    assert resp["status"] == "partial"
    assert resp["failed"] == 1
    per = resp["per_account"][0]
    assert per["status"] == "no_runner"
    assert per["failed"] == 1


def test_cancel_all_state_store_fallback_when_broker_and_cache_empty(monkeypatch):
    """Round-3 P2: when broker get_orders() fails AND _last_orders
    cache is empty, fall back to runner.state_store.get_orders()."""
    broker = _FakeBroker(get_orders_raises=True)
    state_store = SimpleNamespace(
        get_orders=lambda _acct: [_order("persisted-o1")],
    )
    runner = SimpleNamespace(
        broker_account_id="A1",
        is_running=True,
        state_store=state_store,
    )
    runner._broker_client = broker
    runner._last_orders = []  # cache empty
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="state-store-fallback"),
        _mk_request(),
        _mk_admin(),
    ))
    # Persisted order was cancelled even though broker refresh
    # failed and the in-memory cache was empty.
    assert resp["attempted"] == 1
    assert resp["cancelled"] == 1
    assert broker.calls[0]["order_id"] == "persisted-o1"


def test_kill_switch_confirm_clear_passes_scope_id_to_step_up(monkeypatch):
    """Round-3 P1: confirm-clear in LIVE must pass ``resource_id=
    payload.scope_id`` to ``consume_step_up_token`` so a token issued
    for one scope cannot clear another."""
    from app.dashboard.admin_routes import (
        KillSwitchRearmRequest, kill_switch_confirm_clear,
    )
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    captured: List[dict] = []
    monkeypatch.setattr(
        "app.security.step_up.consume_step_up_token",
        lambda **kw: (captured.append(kw), True)[1],
    )
    fake_record = SimpleNamespace(id="rec", state=SimpleNamespace(value="CLEARED"))
    fake_ksm = SimpleNamespace(
        confirm_clear=lambda *a: fake_record,
        _key=lambda *a: ("k",),
        _records={},
        _lock=__import__("threading").RLock(),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: SimpleNamespace(kill_switch_manager=fake_ksm))
    monkeypatch.setattr(admin_routes, "_save_kill_switch_state", lambda *_a, **_kw: None)
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    kill_switch_confirm_clear(
        KillSwitchRearmRequest(
            scope="ACCOUNT",
            scope_id="A1",
            step_up_token="tok-123",
            reason="ack",
        ),
        _mk_admin(),
    )
    assert captured, "step-up token must be consumed"
    assert captured[-1]["resource_id"] == "A1"
    assert captured[-1]["token_id"] == "tok-123"


def test_kill_switch_confirm_clear_persists_reason_in_audit(monkeypatch):
    """Round-3 P2: the operator-entered reason on confirm-clear is
    persisted in audit metadata."""
    from app.dashboard.admin_routes import (
        KillSwitchRearmRequest, kill_switch_confirm_clear,
    )
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    fake_record = SimpleNamespace(id="rec", state=SimpleNamespace(value="CLEARED"))
    fake_ksm = SimpleNamespace(
        confirm_clear=lambda *a: fake_record,
        _key=lambda *a: ("k",),
        _records={},
        _lock=__import__("threading").RLock(),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: SimpleNamespace(kill_switch_manager=fake_ksm))
    monkeypatch.setattr(admin_routes, "_save_kill_switch_state", lambda *_a, **_kw: None)
    captured: List[dict] = []
    monkeypatch.setattr(
        admin_routes, "emit_audit_event",
        lambda **kw: captured.append(kw),
    )
    kill_switch_confirm_clear(
        KillSwitchRearmRequest(
            scope="GLOBAL", scope_id="GLOBAL",
            reason="acknowledged incident X resolved",
        ),
        _mk_admin(),
    )
    assert captured, "audit event must be emitted"
    assert captured[-1]["metadata"]["reason"] == "acknowledged incident X resolved"


def test_kill_switch_state_includes_trade_mode(monkeypatch):
    """Round-3 P2: state endpoint returns ``trade_mode`` so the
    dashboard can conditionally require step-up tokens."""
    from app.dashboard.admin_routes import get_kill_switch_state_endpoint
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setattr(
        "app.risk.kill_switch.get_kill_switch_state",
        lambda: {"records": [], "active_count": 0},
    )
    resp = get_kill_switch_state_endpoint(_mk_admin(AdminRole.OPERATOR))
    assert resp["trade_mode"] == "LIVE"


# ---- PR #240 round-4 review additions ----------------------------------


def test_step_up_issue_rejects_empty_resource_for_kill_switch_clear():
    """Round-4 P1: issuing a ``kill_switch_clear`` token without a
    non-empty ``resource_id`` must raise. Otherwise the token could
    be reused against any scope at consume time."""
    from app.security.step_up import (
        DangerousActionClass, issue_step_up_token,
    )
    with pytest.raises(ValueError) as exc:
        issue_step_up_token(
            actor="admin@phoenix.com",
            action_class=DangerousActionClass.KILL_SWITCH_CLEAR,
            resource_id="",
        )
    assert "resource_id" in str(exc.value)


def test_step_up_issue_rejects_empty_resource_for_kill_switch_rearm():
    """Round-4 P1: same rule for ``kill_switch_rearm``."""
    from app.security.step_up import (
        DangerousActionClass, issue_step_up_token,
    )
    with pytest.raises(ValueError):
        issue_step_up_token(
            actor="admin@phoenix.com",
            action_class=DangerousActionClass.KILL_SWITCH_REARM,
            resource_id="   ",  # whitespace also rejected
        )


def test_step_up_issue_accepts_bound_resource_for_kill_switch():
    """Round-4 P1: non-empty resource binding is accepted."""
    from app.security.step_up import (
        DangerousActionClass, issue_step_up_token, consume_step_up_token,
    )
    tok = issue_step_up_token(
        actor="admin@phoenix.com",
        action_class=DangerousActionClass.KILL_SWITCH_CLEAR,
        resource_id="GLOBAL",
    )
    # Mismatched scope rejected:
    assert not consume_step_up_token(
        token_id=tok.token_id,
        actor="admin@phoenix.com",
        action_class=DangerousActionClass.KILL_SWITCH_CLEAR,
        resource_id="A1",
    )


def test_step_up_consume_rejects_empty_caller_resource_for_kill_switch():
    """Round-4 P1: even if a legacy token with empty stored resource
    exists, consuming a kill-switch action with empty caller
    resource_id must be rejected."""
    from app.security.step_up import (
        DangerousActionClass, _STORE, StepUpToken, consume_step_up_token,
    )
    import time
    tok = StepUpToken(
        token_id="legacy-tok",
        actor="admin@phoenix.com",
        action_class=DangerousActionClass.KILL_SWITCH_CLEAR,
        resource_id="",  # legacy, empty
        issued_at=time.time(),
        expires_at=time.time() + 60,
    )
    _STORE["legacy-tok"] = tok
    try:
        ok = consume_step_up_token(
            token_id="legacy-tok",
            actor="admin@phoenix.com",
            action_class=DangerousActionClass.KILL_SWITCH_CLEAR,
            resource_id="GLOBAL",
        )
        assert ok is False, (
            "legacy token with empty stored resource must NOT be "
            "accepted for kill-switch action classes"
        )
    finally:
        _STORE.pop("legacy-tok", None)


def test_cancel_all_accepts_account_scoped_trip(monkeypatch):
    """Round-4 P2: hierarchical trip-before-cancel — an ACCOUNT-level
    trip at the target broker account satisfies cancel-all even when
    GLOBAL is INACTIVE."""
    broker = _FakeBroker()
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda _request: None,
    )
    from app.risk.kill_switch import KillSwitchScope, KillSwitchState
    account_record = SimpleNamespace(
        scope=KillSwitchScope.ACCOUNT,
        scope_id="A1",
        state=KillSwitchState.TRIPPED,
        block_exits=False,
    )

    def _get_record(scope, scope_id):
        if scope == KillSwitchScope.ACCOUNT and scope_id == "A1":
            return account_record
        return None  # GLOBAL is INACTIVE

    fake_ksm = SimpleNamespace(get_record=_get_record)
    monkeypatch.setattr(admin_routes, "_get_kill_switch_manager", lambda: fake_ksm)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="scoped", broker_account_id="A1"),
        _mk_request(),
        _mk_admin(),
    ))
    assert resp["status"] == "ok"
    assert resp["attempted"] == 1


def test_cancel_all_does_not_fallback_to_cache_after_successful_empty_refresh(monkeypatch):
    """Round-4 P2: when broker.get_orders() succeeds and returns
    [], the in-memory cache must NOT be consulted — a fresh empty
    refresh is authoritative "no open orders"."""
    broker = _FakeBroker(get_orders_result=[])
    # Stale cache (orders that broker confirms no longer exist).
    runner = SimpleNamespace(
        broker_account_id="A1",
        is_running=True,
    )
    runner._broker_client = broker
    runner._last_orders = [_order("stale-o1"), _order("stale-o2")]
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="clean-drain"),
        _mk_request(),
        _mk_admin(),
    ))
    # No cancels — broker confirmed empty open-order set.
    assert resp["attempted"] == 0
    assert resp["status"] == "ok"
    assert len(broker.calls) == 0


def test_cancel_all_audit_includes_per_account_errors(monkeypatch):
    """Round-4 P2: audit metadata must carry per-account error
    details so incident reviewers see which order ids failed."""
    broker = _FakeBroker(raise_for={"o1"})
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    captured: List[dict] = []
    monkeypatch.setattr(
        admin_routes, "emit_audit_event",
        lambda **kw: captured.append(kw),
    )
    _patch_cancel_all_preconditions(monkeypatch)
    _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="audit-errors"),
        _mk_request(),
        _mk_admin(),
    ))
    assert captured
    per_account = captured[-1]["metadata"]["per_account"]
    assert per_account[0]["errors"], (
        "per-account errors must be persisted in audit metadata"
    )
    assert "simulated broker outage" in str(per_account[0]["errors"][0])


def test_kill_switch_rollback_suppressed_on_concurrent_mutation(monkeypatch):
    """Round-4/round-5 P2: if any of (id, state, block_exits,
    updated_at) has changed since the snapshot was taken, the
    rollback must be suppressed so the newer state is preserved.

    Round-5 strengthened the fingerprint from id-only to the full
    tuple because ``KillSwitchManager`` mutates records IN PLACE
    (request_clear/confirm_clear/rearm don't change id), so an
    id-only guard could not detect concurrent state transitions.
    """
    from app.dashboard.admin_routes import _kill_switch_rollback_factory
    from app.risk.kill_switch import KillSwitchScope
    fake_ksm = SimpleNamespace(
        _key=lambda scope, scope_id: (str(scope), str(scope_id)),
        _lock=__import__("threading").RLock(),
        _records={},
    )
    key = ("KillSwitchScope.GLOBAL", "GLOBAL")
    prior = SimpleNamespace(
        id="rec-original", state=SimpleNamespace(value="INACTIVE"),
        block_exits=False, updated_at="2026-05-11T00:00:00Z",
    )
    fake_ksm._records[key] = prior
    rollback = _kill_switch_rollback_factory(
        fake_ksm, KillSwitchScope.GLOBAL, "GLOBAL",
    )
    post_record = SimpleNamespace(
        id="rec-A", state=SimpleNamespace(value="TRIPPED"),
        block_exits=False, updated_at="2026-05-11T00:00:01Z",
    )
    fake_ksm._records[key] = post_record
    rollback.set_expected_post_record(post_record)
    # Concurrent IN-PLACE mutation: same id but different state.
    # An id-only guard would miss this; the full fingerprint catches it.
    in_place_mutated = SimpleNamespace(
        id="rec-A", state=SimpleNamespace(value="CLEAR_PENDING"),
        block_exits=False, updated_at="2026-05-11T00:00:02Z",
    )
    fake_ksm._records[key] = in_place_mutated
    rollback()
    assert fake_ksm._records[key].state.value == "CLEAR_PENDING", (
        "rollback must suppress on in-place state mutation"
    )


def test_step_up_issue_route_translates_value_error_to_422(monkeypatch):
    """Round-5 P2: a kill-switch step-up issuance without
    ``resource_id`` raises ValueError inside ``issue_step_up_token``;
    the route must translate it to HTTP 422 rather than letting it
    escape as a 500."""
    from app.dashboard.admin_routes import StepUpIssueRequest, step_up_issue
    with pytest.raises(HTTPException) as exc:
        step_up_issue(
            StepUpIssueRequest(action_class="kill_switch_clear", resource_id=""),
            _mk_admin(),
        )
    assert exc.value.status_code == 422
    assert "resource_id" in str(exc.value.detail).lower()


def test_cancel_all_accepts_tenant_scoped_trip(monkeypatch):
    """Round-5 P2: a TENANT-level trip that covers the target
    broker account satisfies the precheck — no need to escalate to
    GLOBAL or duplicate the trip at ACCOUNT level."""
    broker = _FakeBroker()
    runner = _mk_runner("A1", [_order("o1")], broker)
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime({"A1": runner}))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda _request: None,
    )
    # Broker account A1 belongs to tenant TENANT_X.
    monkeypatch.setattr(
        "app.tenants.firestore_client.get_broker_account",
        lambda _acct: SimpleNamespace(tenant_id="TENANT_X"),
    )
    from app.risk.kill_switch import KillSwitchScope, KillSwitchState
    tenant_record = SimpleNamespace(
        scope=KillSwitchScope.TENANT,
        scope_id="TENANT_X",
        state=KillSwitchState.TRIPPED,
        block_exits=False,
    )

    def _get_record(scope, scope_id):
        if scope == KillSwitchScope.TENANT and scope_id == "TENANT_X":
            return tenant_record
        return None

    fake_ksm = SimpleNamespace(get_record=_get_record)
    monkeypatch.setattr(admin_routes, "_get_kill_switch_manager", lambda: fake_ksm)
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="tenant", broker_account_id="A1"),
        _mk_request(),
        _mk_admin(),
    ))
    assert resp["status"] == "ok"
    assert resp["attempted"] == 1


def test_cancel_all_response_includes_out_of_scope_field(monkeypatch):
    """Round-5 P2: response includes top-level ``out_of_scope`` count
    so dashboards can render the partial-due-to-scope-filter banner."""
    broker_a = _FakeBroker()
    broker_b = _FakeBroker()
    runners = {
        "ALLOWED": _mk_runner("ALLOWED", [_order("a1")], broker_a),
        "BLOCKED": _mk_runner("BLOCKED", [_order("b1")], broker_b),
    }
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: _mk_runtime(runners))
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kw: None)
    _patch_cancel_all_preconditions(monkeypatch)
    scoped_admin = AdminContext(
        caller="scoped@phoenix.com",
        role=AdminRole.ADMIN,
        broker_account_ids=("ALLOWED",),
        all_tenants=False,
    )
    resp = _run(kill_switch_cancel_all(
        KillSwitchCancelAllRequest(reason="x"),
        _mk_request(),
        scoped_admin,
    ))
    assert resp["out_of_scope"] == 1
    assert resp["status"] == "partial"
