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
    """Records cancel_order calls and returns a configurable response."""

    def __init__(
        self,
        *,
        responses: Optional[List[OrderResponse]] = None,
        raise_for: Optional[set[str]] = None,
    ) -> None:
        self.calls: List[dict] = []
        self._responses: List[OrderResponse] = list(responses or [])
        self._raise_for = set(raise_for or [])

    async def cancel_order(
        self,
        broker_order_id: str,
        *,
        symbol: Optional[str] = None,
        variety: Optional[str] = None,
    ) -> OrderResponse:
        self.calls.append({
            "broker_order_id": broker_order_id,
            "symbol": symbol,
            "variety": variety,
        })
        if broker_order_id in self._raise_for:
            raise RuntimeError(f"simulated broker outage for {broker_order_id}")
        if self._responses:
            return self._responses.pop(0)
        # Default: report cancelled.
        return OrderResponse(
            broker_order_id=broker_order_id,
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
    return runner


def _mk_runtime(runners: dict):
    hub = SimpleNamespace(
        list_runner_ids=lambda: list(runners.keys()),
        get_runner=lambda acct_id: runners.get(acct_id),
    )
    return SimpleNamespace(hub=hub)


def _order(broker_order_id: str, status: str = "OPEN", symbol: str = "X") -> Any:
    return SimpleNamespace(
        broker_order_id=broker_order_id,
        status=status,
        symbol=symbol,
    )


def _run(coro):
    return asyncio.run(coro)


# ---- auth ---------------------------------------------------------------


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
    assert broker.calls[0]["broker_order_id"] == "o3"


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
