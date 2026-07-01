from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.dashboard import admin_routes
from app.dashboard.admin_routes import (
    KillSwitchDurableRepairRequest,
    KillSwitchLegacyRecoveryRequest,
    KillSwitchPasswordClearRequest,
    kill_switch_clear_with_password,
    kill_switch_legacy_recovery_clear,
    kill_switch_repair_durable_from_legacy,
)
from app.dashboard.auth import AdminContext, AdminRole
from app.risk.kill_switch import KillSwitchManager, KillSwitchScope, KillSwitchState


def _admin() -> AdminContext:
    return AdminContext(
        caller="admin@phoenix.com",
        role=AdminRole.ADMIN,
        user_id="admin-user-1",
        email="admin@phoenix.com",
        auth_source="bearer",
        all_tenants=True,
    )


def _request() -> Any:
    return SimpleNamespace(
        headers={"X-Request-Id": "req-test-1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


class _RiskManager:
    def __init__(self) -> None:
        self.kill_switch_activated = True
        self._durable_kill_switch_bridge_succeeded = False
        self._pending_audit_reemit = True
        self.persist_calls = 0

    def _persist_state(self, *, force: bool = True) -> None:
        self.persist_calls += 1


class _StateStore:
    def __init__(self, *, positions: list[Any] | None = None, orders: list[Any] | None = None) -> None:
        self._positions = list(positions or [])
        self._orders = list(orders or [])

    def get_positions(self, _account_id: str) -> list[Any]:
        return list(self._positions)

    def get_orders(self, _account_id: str) -> list[Any]:
        return list(self._orders)


def _runtime(
    *,
    ksm: KillSwitchManager,
    legacy_active: bool = True,
    legacy_reason: str = "floating_drawdown",
    risk_manager: _RiskManager | None = None,
    positions: list[Any] | None = None,
    orders: list[Any] | None = None,
) -> SimpleNamespace:
    legacy = {"active": bool(legacy_active), "reason": legacy_reason}
    rm = risk_manager or _RiskManager()

    def record_legacy_kill_switch_state(*, active: bool, reason: str | None = None) -> None:
        legacy["active"] = bool(active)
        legacy["reason"] = reason or "legacy_risk_manager_inactive"

    def get_legacy_kill_switch_snapshot() -> dict[str, Any]:
        return {
            "publisher_seen": True,
            "legacy_active": bool(legacy["active"]),
            "legacy_reason": legacy["reason"],
        }

    def compute_kill_switch_divergence() -> dict[str, Any]:
        durable_active = ksm.is_tripped(KillSwitchScope.GLOBAL, "GLOBAL")
        return {
            "divergent": bool(legacy["active"]) and not durable_active,
            "legacy_active": bool(legacy["active"]),
            "durable_global_active": durable_active,
            "legacy_reason": legacy["reason"],
            "publisher_seen": True,
        }

    runner = SimpleNamespace(
        broker_account_id="ba1",
        tenant_id="tenant-a",
        _risk_manager=rm,
    )
    hub = SimpleNamespace(
        _runners={"ba1": runner},
        list_runner_ids=lambda: ["ba1"],
        get_runner=lambda account_id: runner if account_id == "ba1" else None,
    )
    return SimpleNamespace(
        kill_switch_manager=ksm,
        hub=hub,
        state_store=_StateStore(positions=positions, orders=orders),
        record_legacy_kill_switch_state=record_legacy_kill_switch_state,
        get_legacy_kill_switch_snapshot=get_legacy_kill_switch_snapshot,
        compute_kill_switch_divergence=compute_kill_switch_divergence,
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, runtime: SimpleNamespace) -> None:
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr("app.hub.runtime.get_hub_runtime", lambda: runtime)


def _patch_admin_common(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[str]:
    secret_file = tmp_path / "admin_kill_switch_override"
    secret_file.write_text("vault-override-password", encoding="utf-8")
    monkeypatch.setenv("ADMIN_KILL_SWITCH_OVERRIDE_FILE", str(secret_file))
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda _request: None)
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **_kw: None)

    async def _post_recheck():
        return {"readyz": {"ready": False, "reason": "kill_switch_active"}}

    monkeypatch.setattr(admin_routes, "_kill_switch_post_recheck", _post_recheck)
    save_states: list[str] = []

    def _save(ksm: KillSwitchManager, *, rollback=None) -> None:
        record = ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL")
        save_states.append(record.state.value if record is not None else "NONE")

    monkeypatch.setattr(admin_routes, "_save_kill_switch_state", _save)
    return save_states


def test_repair_endpoint_creates_durable_global_from_legacy(monkeypatch, tmp_path):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    runtime = _runtime(ksm=ksm, legacy_active=True)
    _patch_runtime(monkeypatch, runtime)
    save_states = _patch_admin_common(monkeypatch, tmp_path)

    result = asyncio.run(
        kill_switch_repair_durable_from_legacy(
            KillSwitchDurableRepairRequest(reason="repair split state"),
            _request(),
            _admin(),
        )
    )

    record = ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL")
    assert result["status"] == "repaired"
    assert record is not None
    assert result["record_id"] == record.id
    assert record.state == KillSwitchState.TRIPPED
    assert record.block_exits is False
    assert record.trip_reason == "durable_repair_from_legacy: floating_drawdown"
    assert result["divergence_before"]["divergent"] is True
    assert result["divergence_after"]["divergent"] is False
    assert save_states == ["TRIPPED"]


def test_repair_endpoint_is_idempotent(monkeypatch, tmp_path):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    runtime = _runtime(ksm=ksm, legacy_active=True)
    _patch_runtime(monkeypatch, runtime)
    save_states = _patch_admin_common(monkeypatch, tmp_path)

    first = asyncio.run(
        kill_switch_repair_durable_from_legacy(
            KillSwitchDurableRepairRequest(reason="repair split state"),
            _request(),
            _admin(),
        )
    )
    second = asyncio.run(
        kill_switch_repair_durable_from_legacy(
            KillSwitchDurableRepairRequest(reason="repair split state again"),
            _request(),
            _admin(),
        )
    )

    assert first["status"] == "repaired"
    assert second["status"] == "already_durable_active"
    assert second["record_id"] == first["record_id"]
    assert len(ksm.get_all_active()) == 1
    assert save_states == ["TRIPPED"]


def test_repair_endpoint_refuses_when_no_legacy_active_state(monkeypatch, tmp_path):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    runtime = _runtime(ksm=ksm, legacy_active=False, legacy_reason="cleared")
    _patch_runtime(monkeypatch, runtime)
    _patch_admin_common(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            kill_switch_repair_durable_from_legacy(
                KillSwitchDurableRepairRequest(reason="repair split state"),
                _request(),
                _admin(),
            )
        )

    assert exc_info.value.status_code == 409
    assert ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL") is None


def test_legacy_recovery_clear_refuses_divergence_until_repaired(
    monkeypatch,
    tmp_path,
):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    risk_manager = _RiskManager()
    runtime = _runtime(
        ksm=ksm,
        legacy_active=True,
        risk_manager=risk_manager,
        positions=[{"symbol": "NIFTY", "quantity": 0}],
        orders=[{"order_id": "ord1", "status": "COMPLETE"}],
    )
    _patch_runtime(monkeypatch, runtime)
    _patch_admin_common(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            kill_switch_legacy_recovery_clear(
                KillSwitchLegacyRecoveryRequest(
                    password="vault-override-password",
                    reason="broker flat verified",
                ),
                _request(),
                _admin(),
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["next_step"] == (
        "Run /admin/kill-switch/repair-durable-from-legacy first"
    )
    assert risk_manager.kill_switch_activated is True

    asyncio.run(
        kill_switch_repair_durable_from_legacy(
            KillSwitchDurableRepairRequest(reason="repair before clear"),
            _request(),
            _admin(),
        )
    )
    result = asyncio.run(
        kill_switch_legacy_recovery_clear(
            KillSwitchLegacyRecoveryRequest(
                password="vault-override-password",
                reason="broker flat verified",
            ),
            _request(),
            _admin(),
        )
    )

    assert result["status"] == "inactive"
    assert result["durable_transitions"] == ["CLEAR_PENDING", "CLEARED", "INACTIVE"]
    assert risk_manager.kill_switch_activated is False
    assert risk_manager.persist_calls == 1
    assert ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL").state == KillSwitchState.INACTIVE


def test_clear_with_password_remains_blocked_by_legacy_active_state(
    monkeypatch,
    tmp_path,
):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    ksm.trip(
        KillSwitchScope.GLOBAL,
        "GLOBAL",
        "auto-trip",
        actor="risk_manager_auto",
    )
    runtime = _runtime(ksm=ksm, legacy_active=True)
    _patch_runtime(monkeypatch, runtime)
    _patch_admin_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        admin_routes,
        "_collect_position_authority_clear_failures",
        lambda *, fail_closed: [],
    )

    with pytest.raises(HTTPException) as exc_info:
        kill_switch_clear_with_password(
            KillSwitchPasswordClearRequest(
                password="vault-override-password",
                reason="broker flat verified",
            ),
            _request(),
            _admin(),
        )

    assert exc_info.value.status_code == 409
    assert "legacy-recovery-clear" in str(exc_info.value.detail)
    assert ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL").state == KillSwitchState.TRIPPED
