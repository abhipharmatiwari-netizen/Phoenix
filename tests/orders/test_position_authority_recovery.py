from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from app.brokers.base import ProductType
from app.data.state_store import StateStore
from app.orders import position_authority_recovery as recovery


class _Lifecycle:
    def __init__(self, records):
        self.records = {record.ownership_key: record for record in records}

    def list_position_records(self):
        return [copy.deepcopy(record) for record in self.records.values()]

    def force_clear_position_record(self, *, scope_key: str, reason: str):
        record = self.records.get(scope_key)
        if record is None:
            return None
        prior = copy.deepcopy(record)
        record.position_state = "FLAT"
        record.net_qty = 0.0
        record.unrealized_pnl = 0.0
        record.state_reason = reason
        return prior


def _record(*, state: str = "RECONCILING", net_qty: float = 0.0):
    contract_key = repr(("NIFTY", "2026-05-26", "24000", "CE", "CARRY_FORWARD"))
    return SimpleNamespace(
        tenant_id="tenant-1",
        account_id="A1",
        strategy_id="system::position_trailing_lock",
        ownership_key=(
            "tenant-1:A1:system::position_trailing_lock:CARRY_FORWARD:"
            f"{contract_key}"
        ),
        contract_key=contract_key,
        position_state=state,
        state_reason="placement_terminal_non_fill_rejected",
        side="BUY",
        net_qty=net_qty,
        unrealized_pnl=0.0,
    )


def _mark_broker_snapshots_fresh(state_store: StateStore, account_id: str = "A1"):
    position_ts = datetime.now(timezone.utc).isoformat()
    state_store.update_positions_status(
        account_id,
        status="OK",
        last_ok_ts=position_ts,
        last_count=0,
        error_reason=None,
        blocked_ts=None,
        retry_after_seconds=None,
    )
    state_store.update_orders_ok_ts(
        account_id,
        last_ok_ts=datetime.now(timezone.utc).isoformat(),
    )


def test_auto_recovers_zero_qty_record_when_broker_contract_is_flat(monkeypatch):
    monkeypatch.setattr(recovery, "emit_audit_event", lambda **_kw: None)
    monkeypatch.setattr(recovery, "_persist_flat_clear", lambda **_kw: 0)

    state_store = StateStore()
    state_store.set_positions("A1", [])
    _mark_broker_snapshots_fresh(state_store)
    lifecycle = _Lifecycle([_record()])

    result = recovery.auto_recover_broker_flat_zero_qty_records(
        lifecycle=lifecycle,
        state_store=state_store,
        broker_account_id="A1",
    )

    assert result["recovered"] == 1
    record = next(iter(lifecycle.records.values()))
    assert record.position_state == "FLAT"
    assert record.state_reason == "broker_flat_auto_recovery"


def test_auto_recovers_zero_qty_flat_pending_confirmation(monkeypatch):
    monkeypatch.setattr(recovery, "emit_audit_event", lambda **_kw: None)
    monkeypatch.setattr(recovery, "_persist_flat_clear", lambda **_kw: 0)

    state_store = StateStore()
    state_store.set_positions("A1", [])
    _mark_broker_snapshots_fresh(state_store)
    lifecycle = _Lifecycle([_record(state="FLAT_PENDING_CONFIRMATION")])

    result = recovery.auto_recover_broker_flat_zero_qty_records(
        lifecycle=lifecycle,
        state_store=state_store,
        broker_account_id="A1",
    )

    assert result["recovered"] == 1
    record = next(iter(lifecycle.records.values()))
    assert record.position_state == "FLAT"
    assert record.state_reason == "broker_flat_auto_recovery"


def test_auto_recovery_waits_for_fresh_order_snapshot(monkeypatch):
    monkeypatch.setattr(recovery, "emit_audit_event", lambda **_kw: None)
    monkeypatch.setattr(recovery, "_persist_flat_clear", lambda **_kw: 0)

    state_store = StateStore()
    state_store.set_positions("A1", [])
    state_store.update_positions_status(
        "A1",
        status="OK",
        last_ok_ts=datetime.now(timezone.utc).isoformat(),
        last_count=0,
        error_reason=None,
        blocked_ts=None,
        retry_after_seconds=None,
    )
    lifecycle = _Lifecycle([_record()])

    result = recovery.auto_recover_broker_flat_zero_qty_records(
        lifecycle=lifecycle,
        state_store=state_store,
        broker_account_id="A1",
    )

    assert result["recovered"] == 0
    assert result["skipped"][0]["reason"] == "orders_snapshot_not_synced"
    record = next(iter(lifecycle.records.values()))
    assert record.position_state == "RECONCILING"


def test_auto_recovery_does_not_clear_when_broker_position_exists(monkeypatch):
    monkeypatch.setattr(recovery, "emit_audit_event", lambda **_kw: None)
    monkeypatch.setattr(recovery, "_persist_flat_clear", lambda **_kw: 0)

    contract_key = repr(("NIFTY", "2026-05-26", "24000", "CE", "CARRY_FORWARD"))
    state_store = StateStore()
    state_store.set_positions(
        "A1",
        [
            SimpleNamespace(
                symbol="NIFTY26MAY2624000CE",
                quantity=-65,
                avg_price=79.9,
                product_type=ProductType.CARRY_FORWARD,
                contract_key=contract_key,
            )
        ],
    )
    _mark_broker_snapshots_fresh(state_store)
    lifecycle = _Lifecycle([_record()])

    result = recovery.auto_recover_broker_flat_zero_qty_records(
        lifecycle=lifecycle,
        state_store=state_store,
        broker_account_id="A1",
    )

    assert result["recovered"] == 0
    assert result["skipped"][0]["reason"] == "broker_position_nonzero"
    record = next(iter(lifecycle.records.values()))
    assert record.position_state == "RECONCILING"
