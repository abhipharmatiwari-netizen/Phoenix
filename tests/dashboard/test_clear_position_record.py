"""Tests for the /admin/state/clear-position-record endpoint and the
underlying OrderLifecycleService.force_clear_position_record() helper.

This endpoint replaces the manual stop-backend / hand-edit-Postgres /
start-backend dance documented in
ops/cleanup_a1_stuck_state_20260507.sql for clearing a position record
that has gotten stuck non-terminal (e.g. flip-fill RECOVERY_PENDING residue).
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from fastapi import HTTPException

from app.brokers.base import Position, ProductType
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.position_state import InternalPosition, PositionState
from app.dashboard.admin_routes import (
    ClearPositionRecordRequest,
    clear_position_record,
)
from app.dashboard.auth import AdminContext, AdminRole
from app.orders.order_lifecycle import OrderLifecycleService, SubmissionContext
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


# ---- service-layer (force_clear_position_record) -----------------------

def _mk_lifecycle() -> OrderLifecycleService:
    class _NoopStateStore:
        def get_balance(self, *_a, **_kw): return None
        def get_positions(self, *_a, **_kw): return []
        def set_positions(self, *_a, **_kw): return None
    return OrderLifecycleService(
        state_store=_NoopStateStore(),
        pnl_engine=None,
        position_ownership_store=None,
        processed_store=InMemoryProcessedTradeStore(),
    )


def _seed_record(svc: OrderLifecycleService, *, scope_key: str) -> InternalPosition:
    """Place a stuck-style record matching the 2026-05-07 A1 incident shape."""
    rec = InternalPosition(
        tenant_id="tenant-1",
        account_id="A1",
        strategy_id="ema20_strategy",
        ownership_key=scope_key,
        contract_key="('NG', '2026-05-22', '255', 'CE', 'INTRADAY')",
        side="SELL",
        net_qty=-1250.0,
        filled_qty_open=1250.0,
        filled_qty_close=2500.0,
        avg_open_price=10.70,
        avg_close_price=12.50,
        position_state=PositionState.RECOVERY_PENDING,
        state_reason=(
            "illegal_transition_blocked:RECONCILING_to_PARTIALLY_EXITED:"
            "partial_exit_fill_observed"
        ),
        opened_by_order_id="260507001433140",
    )
    svc._position_records[scope_key] = rec
    return rec


def test_force_clear_returns_prior_record_and_marks_flat():
    svc = _mk_lifecycle()
    scope = "tenant-1:A1:ema20_strategy:INTRADAY:('NG', '2026-05-22', '255', 'CE', 'INTRADAY')"
    rec = _seed_record(svc, scope_key=scope)

    prior = svc.force_clear_position_record(scope_key=scope, reason="manual_recovery_post_flip")
    assert prior is not None
    assert prior.position_state == PositionState.RECOVERY_PENDING  # snapshot of BEFORE
    assert prior.net_qty == -1250.0

    # In-memory record is now FLAT.
    assert rec.position_state == PositionState.FLAT
    assert rec.state_reason == "manual_recovery_post_flip"
    assert rec.last_reconciled_at is not None


def test_force_clear_returns_none_when_record_missing():
    svc = _mk_lifecycle()
    out = svc.force_clear_position_record(scope_key="nonexistent", reason="x")
    assert out is None


def test_force_clear_bypasses_state_machine_transitions_from_terminal_state():
    """Even if the record is already FLAT, force_clear is a no-throw idempotent
    operation (admin endpoint can call it repeatedly without errors)."""
    svc = _mk_lifecycle()
    scope = "scope-flat"
    rec = InternalPosition(
        tenant_id="tenant-1", account_id="A1",
        strategy_id="x", ownership_key=scope,
        position_state=PositionState.FLAT,
    )
    svc._position_records[scope] = rec
    prior = svc.force_clear_position_record(scope_key=scope, reason="re-clear")
    assert prior is not None
    assert rec.position_state == PositionState.FLAT
    assert rec.state_reason == "re-clear"


# ---- HTTP-layer (clear_position_record route) -------------------------

class _RecordingStateStore:
    def __init__(self, positions: List[Any]) -> None:
        self._positions = positions

    def get_positions(self, _broker_account_id):
        return list(self._positions)


def _mk_admin_ctx(role: AdminRole = AdminRole.OPERATOR) -> AdminContext:
    return AdminContext(caller="ops@phoenix.com", role=role)


def _mk_request_stub() -> Any:
    # Stand-in for fastapi.Request — used by check_rate_limit (reads .client)
    # and by _request_id_from_request (reads .headers).
    return SimpleNamespace(
        headers={"X-Request-Id": "req-test-1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _patch_rate_limit(monkeypatch) -> None:
    """check_rate_limit is imported by name into admin_routes; patch the
    bound name there so our test stub bypasses the real (Redis-backed) one."""
    monkeypatch.setattr(
        "app.dashboard.admin_routes.check_rate_limit",
        lambda _request: None,
    )


def test_clear_route_404_when_no_record(monkeypatch):
    svc = _mk_lifecycle()
    runtime = SimpleNamespace(order_lifecycle=svc, state_store=_RecordingStateStore([]))
    monkeypatch.setattr(
        "app.dashboard.admin_routes.get_hub_runtime", lambda: runtime
    )
    _patch_rate_limit(monkeypatch)

    payload = ClearPositionRecordRequest(
        scope_key="does-not-exist", reason="test", force=False
    )
    with pytest.raises(HTTPException) as exc:
        clear_position_record(_mk_request_stub(), payload, _mk_admin_ctx())
    assert exc.value.status_code == 404


def test_clear_route_409_when_broker_still_has_position(monkeypatch):
    svc = _mk_lifecycle()
    scope = "tenant-1:A1:ema20_strategy:INTRADAY:('NG', '2026-05-22', '255', 'CE', 'INTRADAY')"
    _seed_record(svc, scope_key=scope)

    # Broker reports an OPEN position with matching contract_key.
    open_pos = SimpleNamespace(
        symbol="NATURALGAS22MAY26255CE",
        quantity=1250,
        avg_price=14.30,
        contract_key="('NG', '2026-05-22', '255', 'CE', 'INTRADAY')",
    )
    runtime = SimpleNamespace(
        order_lifecycle=svc,
        state_store=_RecordingStateStore([open_pos]),
    )
    monkeypatch.setattr(
        "app.dashboard.admin_routes.get_hub_runtime", lambda: runtime
    )
    _patch_rate_limit(monkeypatch)

    payload = ClearPositionRecordRequest(scope_key=scope, reason="test", force=False)
    with pytest.raises(HTTPException) as exc:
        clear_position_record(_mk_request_stub(), payload, _mk_admin_ctx())
    assert exc.value.status_code == 409
    assert "force=true" in str(exc.value.detail).lower() or "force" in str(exc.value.detail).lower()


def test_clear_route_force_overrides_safety_check(monkeypatch):
    svc = _mk_lifecycle()
    scope = "tenant-1:A1:ema20_strategy:INTRADAY:('NG', '2026-05-22', '255', 'CE', 'INTRADAY')"
    rec = _seed_record(svc, scope_key=scope)

    open_pos = SimpleNamespace(
        symbol="NATURALGAS22MAY26255CE",
        quantity=1250,
        contract_key="('NG', '2026-05-22', '255', 'CE', 'INTRADAY')",
    )
    runtime = SimpleNamespace(
        order_lifecycle=svc,
        state_store=_RecordingStateStore([open_pos]),
    )
    monkeypatch.setattr(
        "app.dashboard.admin_routes.get_hub_runtime", lambda: runtime
    )
    _patch_rate_limit(monkeypatch)
    # Stub Postgres path so the test doesn't need a live DB.
    monkeypatch.setattr(
        "app.data.postgres.connect_with_retry",
        lambda *_a, **_kw: _StubConn(),
    )
    monkeypatch.setattr(
        "app.data.postgres.get_control_plane_dsn",
        lambda *_a, **_kw: "postgresql://stub",
    )

    payload = ClearPositionRecordRequest(scope_key=scope, reason="break_glass", force=True)
    out = clear_position_record(_mk_request_stub(), payload, _mk_admin_ctx())
    assert out["status"] == "ok"
    assert rec.position_state == PositionState.FLAT


def test_clear_route_clean_path_when_broker_is_flat(monkeypatch):
    svc = _mk_lifecycle()
    scope = "tenant-1:A1:ema20_strategy:INTRADAY:('NG', '2026-05-22', '255', 'CE', 'INTRADAY')"
    rec = _seed_record(svc, scope_key=scope)

    runtime = SimpleNamespace(
        order_lifecycle=svc,
        state_store=_RecordingStateStore([]),  # no positions on broker
    )
    monkeypatch.setattr(
        "app.dashboard.admin_routes.get_hub_runtime", lambda: runtime
    )
    _patch_rate_limit(monkeypatch)
    monkeypatch.setattr(
        "app.data.postgres.connect_with_retry",
        lambda *_a, **_kw: _StubConn(),
    )
    monkeypatch.setattr(
        "app.data.postgres.get_control_plane_dsn",
        lambda *_a, **_kw: "postgresql://stub",
    )

    payload = ClearPositionRecordRequest(scope_key=scope, reason="manual_post_flat", force=False)
    out = clear_position_record(_mk_request_stub(), payload, _mk_admin_ctx())
    assert out["status"] == "ok"
    assert out["scope_key"] == scope
    assert out["prior_state"] == "RECOVERY_PENDING"
    assert rec.position_state == PositionState.FLAT


def test_clear_route_403_when_role_insufficient(monkeypatch):
    svc = _mk_lifecycle()
    scope = "tenant-1:A1:ema20_strategy:INTRADAY:('NG', '2026-05-22', '255', 'CE', 'INTRADAY')"
    _seed_record(svc, scope_key=scope)

    runtime = SimpleNamespace(order_lifecycle=svc, state_store=_RecordingStateStore([]))
    monkeypatch.setattr(
        "app.dashboard.admin_routes.get_hub_runtime", lambda: runtime
    )
    _patch_rate_limit(monkeypatch)

    payload = ClearPositionRecordRequest(scope_key=scope, reason="x", force=False)
    with pytest.raises(HTTPException) as exc:
        clear_position_record(
            _mk_request_stub(),
            payload,
            _mk_admin_ctx(role=AdminRole.READONLY),
        )
    assert exc.value.status_code == 403


# ---- helpers ----------------------------------------------------------

class _StubCursor:
    def __init__(self) -> None:
        self.rowcount = 0
    def execute(self, *_a, **_kw): return None
    def fetchone(self): return None
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *_a): return False


class _StubConn:
    def cursor(self): return _StubCursor()
    def commit(self): return None
    def close(self): return None
    def __enter__(self): return self
    def __exit__(self, *_a): return False
