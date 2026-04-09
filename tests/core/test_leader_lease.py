from __future__ import annotations

import pytest

import app.core.leader_lease as leader_lease_mod
from app.core.leader_lease import LeaderLease


class _FakeCursor:
    def __init__(self, responses: list[tuple[object, ...]]) -> None:
        self._responses = responses
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, tuple(params) if params is not None else None))

    def fetchone(self):
        if self._responses:
            return self._responses.pop(0)
        return None


class _FakeConn:
    def __init__(self, responses: list[tuple[object, ...]]) -> None:
        self._responses = responses
        self.closed = False
        self.cursors: list[_FakeCursor] = []

    def cursor(self):
        cursor = _FakeCursor(self._responses)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_leader_lease_renew_failure_triggers_process_exit(monkeypatch):
    lease = LeaderLease(lease_id="lease-1", enabled=True)
    lease._owned = True

    exit_calls: list[int] = []

    async def _fast_sleep(_: float) -> None:
        return None

    monkeypatch.setenv("LEADER_LEASE_EXIT_ON_LOSS", "true")
    monkeypatch.setattr("app.core.leader_lease.asyncio.sleep", _fast_sleep)
    monkeypatch.setattr(lease, "_renew_sync", lambda: False)
    monkeypatch.setattr(
        "app.core.leader_lease.os._exit",
        lambda code: exit_calls.append(int(code)),
    )

    await lease._renew_loop()

    assert exit_calls == [2]
    assert lease.status_snapshot()["renew_failures"] == 1
    assert lease.status_snapshot()["last_failure_at"] is not None


def test_leader_lease_status_snapshot_reports_runtime_fields():
    lease = LeaderLease(
        lease_id="lease-2",
        ttl_seconds=45,
        renew_seconds=15,
        enabled=True,
        owner_id="worker-a",
    )
    snapshot = lease.status_snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["lease_id"] == "lease-2"
    assert snapshot["owner_id"] == "worker-a"
    assert snapshot["ttl_seconds"] == 45
    assert snapshot["renew_seconds"] == 15
    assert snapshot["task_running"] is False


def test_postgres_leader_lease_acquires_and_releases_advisory_lock(monkeypatch):
    conn = _FakeConn(responses=[(True,), (True,)])
    monkeypatch.setattr(leader_lease_mod, "connect_with_retry", lambda *_, **__: conn)

    lease = LeaderLease(
        lease_id="phoenix-live-single-stack",
        enabled=True,
        backend="postgres",
        postgres_dsn="postgresql://example",
    )

    assert lease._try_acquire_sync() is True
    assert lease.status_snapshot()["backend"] == "postgres"
    assert lease.status_snapshot()["connection_open"] is True

    lease._release_sync()

    executed_sql = [sql for cursor in conn.cursors for sql, _ in cursor.executed]
    assert any("pg_try_advisory_lock" in sql for sql in executed_sql)
    assert any("pg_advisory_unlock" in sql for sql in executed_sql)
    assert conn.closed is True


def test_postgres_leader_lease_returns_false_when_lock_is_held(monkeypatch):
    conn = _FakeConn(responses=[(False,)])
    monkeypatch.setattr(leader_lease_mod, "connect_with_retry", lambda *_, **__: conn)

    lease = LeaderLease(
        lease_id="phoenix-live-single-stack",
        enabled=True,
        backend="postgres",
        postgres_dsn="postgresql://example",
    )

    assert lease._try_acquire_sync() is False
    assert conn.closed is True
