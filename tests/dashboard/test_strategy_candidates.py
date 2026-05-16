from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.dashboard import admin_routes, strategy_candidates
from app.dashboard.auth import AdminContext, AdminRole, get_admin_context
from scripts.replay.optimizer_runtime import DEFAULT_PARAMS


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self.rowcount = 1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if "UPDATE public.strategy_config_candidates" in sql:
            self.rowcount = self.conn.candidate_update_rowcount
        self.conn.statements.append((sql, dict(params or {})))

    def fetchone(self) -> Any:
        return self.conn.fetchone_rows.pop(0)

    def fetchall(self) -> list[Any]:
        return self.conn.fetchall_rows.pop(0)


class FakeConnection:
    def __init__(
        self,
        *,
        fetchone_rows: list[Any] | None = None,
        fetchall_rows: list[list[Any]] | None = None,
        candidate_update_rowcount: int = 1,
    ) -> None:
        self.fetchone_rows = list(fetchone_rows or [])
        self.fetchall_rows = list(fetchall_rows or [])
        self.candidate_update_rowcount = candidate_update_rowcount
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _candidate_row(
    *,
    candidate_id: str = "cand-1",
    strategy_config_id: str = "cfg-1",
    candidate_params: dict[str, Any] | None = None,
    current_params: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    status: str = "pending",
    created_at: datetime | None = None,
    reviewed_at: datetime | None = None,
    reviewed_by: str | None = None,
    strategy_id: str = "ema20_strategy",
) -> tuple[Any, ...]:
    now = datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc)
    return (
        candidate_id,
        strategy_config_id,
        candidate_params or {"sl_pct": 0.2, "tp_pct": 0.35},
        metrics or {"score": 1.4, "total_pnl": 1200.0, "win_rate": 0.58},
        "[2026-05-01,2026-05-10)",
        "optimizer-sha",
        created_at or now,
        reviewed_at,
        reviewed_by,
        status,
        "tenant-1",
        "acct-1",
        strategy_id,
        True,
        current_params or {"underlying_label": "NIFTY_IDX", "sl_pct": 0.3},
        now,
    )


def _patch_connection(monkeypatch: pytest.MonkeyPatch, conn: FakeConnection) -> None:
    monkeypatch.setattr(strategy_candidates, "get_control_plane_dsn", lambda: "postgres://test")
    monkeypatch.setattr(
        strategy_candidates,
        "connect_with_retry",
        lambda _dsn, *, autocommit=True: conn,
    )


def test_list_strategy_candidates_includes_diff_and_status_filter(monkeypatch: pytest.MonkeyPatch):
    conn = FakeConnection(fetchall_rows=[[_candidate_row()]])
    _patch_connection(monkeypatch, conn)

    payload = strategy_candidates.list_strategy_candidates(
        candidate_status="pending",
        limit=25,
    )

    assert payload["count"] == 1
    candidate = payload["candidates"][0]
    assert candidate["candidate_id"] == "cand-1"
    assert candidate["param_diff"]["sl_pct"] == {"current": 0.3, "candidate": 0.2}
    assert candidate["current_params"]["underlying_label"] == "NIFTY_IDX"
    assert conn.statements[0][1] == {"limit": 25, "status": "pending"}


def test_approve_promotes_candidate_and_preserves_non_optimizer_params(
    monkeypatch: pytest.MonkeyPatch,
):
    after_row = _candidate_row(
        status="promoted",
        reviewed_at=datetime(2026, 5, 17, 3, 5, tzinfo=timezone.utc),
        reviewed_by="admin",
        current_params={"underlying_label": "NIFTY_IDX", "sl_pct": 0.2, "tp_pct": 0.35},
    )
    conn = FakeConnection(fetchone_rows=[_candidate_row(), after_row])
    _patch_connection(monkeypatch, conn)
    audit_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        strategy_candidates,
        "emit_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    result = strategy_candidates.approve_strategy_candidate(
        candidate_id="cand-1",
        actor="admin",
        reason="better score",
        request_id="req-1",
        now=datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc),
    )

    update_stmt = next(
        params
        for sql, params in conn.statements
        if "UPDATE public.strategy_configs" in sql
    )
    promoted_params = json.loads(update_stmt["params"])
    assert promoted_params == {
        "underlying_label": "NIFTY_IDX",
        "sl_pct": 0.2,
        "tp_pct": 0.35,
    }
    assert result["status"] == "promoted"
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert audit_events[0]["action"] == "approve_strategy_candidate"
    assert audit_events[0]["after"]["params"]["underlying_label"] == "NIFTY_IDX"


def test_approve_rejects_unknown_candidate_param(monkeypatch: pytest.MonkeyPatch):
    conn = FakeConnection(
        fetchone_rows=[
            _candidate_row(candidate_params={"sl_pct": 0.2, "unsafe_key": True})
        ]
    )
    _patch_connection(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        strategy_candidates.approve_strategy_candidate(
            candidate_id="cand-1",
            actor="admin",
            reason=None,
            request_id=None,
            now=datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc),
        )

    assert exc_info.value.status_code == 422
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert not any("UPDATE public.strategy_configs" in sql for sql, _ in conn.statements)


def test_approve_rejects_stale_candidate(monkeypatch: pytest.MonkeyPatch):
    conn = FakeConnection(
        fetchone_rows=[
            _candidate_row(created_at=datetime(2026, 5, 8, 3, 0, tzinfo=timezone.utc))
        ]
    )
    _patch_connection(monkeypatch, conn)

    with pytest.raises(HTTPException) as exc_info:
        strategy_candidates.approve_strategy_candidate(
            candidate_id="cand-1",
            actor="admin",
            reason=None,
            request_id=None,
            now=datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc),
        )

    assert exc_info.value.status_code == 409
    assert "older than 7 days" in str(exc_info.value.detail)
    assert conn.rollbacks == 1


def test_reject_does_not_update_strategy_params(monkeypatch: pytest.MonkeyPatch):
    after_row = _candidate_row(
        status="rejected",
        reviewed_at=datetime(2026, 5, 17, 3, 5, tzinfo=timezone.utc),
        reviewed_by="admin",
    )
    conn = FakeConnection(
        fetchone_rows=[
            _candidate_row(candidate_params={"unknown_future_key": "bad"}),
            after_row,
        ]
    )
    _patch_connection(monkeypatch, conn)
    audit_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        strategy_candidates,
        "emit_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    result = strategy_candidates.reject_strategy_candidate(
        candidate_id="cand-1",
        actor="admin",
        reason="schema mismatch",
        request_id="req-2",
        now=datetime(2026, 5, 17, 3, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "rejected"
    assert not any("UPDATE public.strategy_configs" in sql for sql, _ in conn.statements)
    assert any("UPDATE public.strategy_config_candidates" in sql for sql, _ in conn.statements)
    assert audit_events[0]["action"] == "reject_strategy_candidate"


def test_allowed_candidate_param_keys_cover_replay_defaults():
    allowed = dict(strategy_candidates.iter_allowed_param_keys())

    for strategy_id, defaults in DEFAULT_PARAMS.items():
        assert set(defaults).issubset(allowed[strategy_id])


def _build_admin_app(role: AdminRole) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[get_admin_context] = (
        lambda: AdminContext(caller="test-admin", role=role)
    )
    return app


def test_strategy_candidate_routes_enforce_admin_for_review(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda _request: None)
    monkeypatch.setattr(
        admin_routes,
        "list_strategy_candidates",
        lambda *, candidate_status, limit: {"count": 0, "candidates": []},
    )
    monkeypatch.setattr(
        admin_routes,
        "approve_strategy_candidate",
        lambda **_kwargs: {"candidate_id": "cand-1", "status": "promoted"},
    )

    readonly_client = TestClient(_build_admin_app(AdminRole.READONLY))
    assert readonly_client.get("/admin/strategy-candidates").status_code == 200
    assert (
        readonly_client.post(
            "/admin/strategy-candidates/cand-1/approve",
            json={"reason": "reviewed"},
        ).status_code
        == 403
    )

    admin_client = TestClient(_build_admin_app(AdminRole.ADMIN))
    resp = admin_client.post(
        "/admin/strategy-candidates/cand-1/approve",
        json={"reason": "reviewed"},
        headers={"X-Request-Id": "req-route"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "promoted"
