from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app import server
from app.risk.kill_switch import (
    KillSwitchClearRequest,
    KillSwitchClearValidation,
    KillSwitchManager,
    KillSwitchScope,
    repair_durable_global_from_legacy,
)


def _runtime(
    *,
    ksm: KillSwitchManager,
    legacy_active: bool,
    legacy_reason: str = "floating_drawdown",
) -> SimpleNamespace:
    legacy = {"active": bool(legacy_active), "reason": legacy_reason}

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

    def record_legacy_kill_switch_state(*, active: bool, reason: str | None = None) -> None:
        legacy["active"] = bool(active)
        legacy["reason"] = reason or "legacy_risk_manager_inactive"

    return SimpleNamespace(
        kill_switch_manager=ksm,
        get_legacy_kill_switch_snapshot=get_legacy_kill_switch_snapshot,
        compute_kill_switch_divergence=compute_kill_switch_divergence,
        record_legacy_kill_switch_state=record_legacy_kill_switch_state,
    )


def _patch_runtime(monkeypatch, runtime: SimpleNamespace) -> None:
    monkeypatch.setattr(server, "_readiness_trade_mode", lambda: "LIVE")
    monkeypatch.setattr("app.hub.runtime.get_hub_runtime", lambda: runtime)


def test_divergence_keeps_readyz_snapshot_not_ready(monkeypatch):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    runtime = _runtime(ksm=ksm, legacy_active=True)
    _patch_runtime(monkeypatch, runtime)

    snapshot = server._kill_switch_readiness_snapshot()

    assert snapshot["ready"] is False
    assert snapshot["reason"] == "kill_switch_divergence: legacy=True durable_global=False"
    assert snapshot["degraded_reason"] == "kill_switch_divergence"
    assert snapshot["divergent"] is True


def test_after_durable_repair_readyz_reason_is_durable_active(monkeypatch):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    runtime = _runtime(ksm=ksm, legacy_active=True)
    _patch_runtime(monkeypatch, runtime)

    repair_durable_global_from_legacy(
        ksm,
        runtime.get_legacy_kill_switch_snapshot(),
        actor="admin@phoenix.com",
        reason="repair split state",
    )
    snapshot = server._kill_switch_readiness_snapshot()

    assert snapshot["ready"] is False
    assert snapshot["reason"] == "kill_switch_active: 1 non-INACTIVE kill switch(es)"
    assert snapshot["degraded_reason"] == "kill_switch_active"
    assert snapshot["divergent"] is False


def test_after_clear_rearm_and_legacy_clear_readyz_snapshot_can_be_green(monkeypatch):
    ksm = KillSwitchManager(audit_fn=lambda **_kw: None)
    runtime = _runtime(ksm=ksm, legacy_active=True)
    _patch_runtime(monkeypatch, runtime)
    repair_durable_global_from_legacy(
        ksm,
        runtime.get_legacy_kill_switch_snapshot(),
        actor="admin@phoenix.com",
        reason="repair split state",
    )
    clear_req = KillSwitchClearRequest(
        scope=KillSwitchScope.GLOBAL,
        scope_id="GLOBAL",
        actor="admin@phoenix.com",
        reason_code="broker_flat_verified",
        request_id="clear-1",
    )
    record, validation = ksm.request_clear(
        clear_req,
        lambda _req: KillSwitchClearValidation(passed=True, failures=[]),
    )
    assert validation.passed is True
    record = ksm.confirm_clear(KillSwitchScope.GLOBAL, "GLOBAL")
    assert record.state.value == "CLEARED"
    ksm.rearm(KillSwitchScope.GLOBAL, "GLOBAL", actor="admin@phoenix.com")
    runtime.record_legacy_kill_switch_state(
        active=False,
        reason="admin_legacy_recovery:broker_flat_verified",
    )

    snapshot = server._kill_switch_readiness_snapshot()

    assert snapshot["ready"] is True
    assert snapshot["reason"] is None
    assert snapshot["degraded_reason"] is None
    assert snapshot["active_count"] == 0
    assert snapshot["divergent"] is False
