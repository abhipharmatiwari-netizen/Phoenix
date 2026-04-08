from __future__ import annotations

import pytest

from app.core.leader_lease import LeaderLease


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
