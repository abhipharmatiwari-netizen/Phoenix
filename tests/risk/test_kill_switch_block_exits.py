"""Tests for issue #220: KillSwitchManager block_exits flag.

Adds unit-level coverage for:
- ``KillSwitchManager.trip(..., block_exits=...)`` records the flag.
- ``KillSwitchRecord.block_exits`` defaults to False (preserves SOFT-trip
  behaviour for legacy callers and the auto-trip bridge).
- ``is_tripped_for_scope_with_block_exits(...)`` returns the second
  element True only if any matching active record has block_exits True.
- Persistence (to_persistence_dict / from_persistence_dict) round-trips
  the flag and tolerates rows from a pre-migration schema.
"""

from __future__ import annotations

import pytest

from app.risk.kill_switch import (
    KillSwitchManager,
    KillSwitchScope,
    KillSwitchState,
)


def test_trip_default_block_exits_is_false():
    ksm = KillSwitchManager()
    record = ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "test", actor="op",
    )
    assert record.block_exits is False


def test_trip_with_block_exits_true_records_flag():
    ksm = KillSwitchManager()
    record = ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "panic", actor="op",
        block_exits=True,
    )
    assert record.block_exits is True


def test_is_tripped_for_scope_with_block_exits_soft():
    ksm = KillSwitchManager()
    ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", "soft", actor="op")
    tripped, block_exits = ksm.is_tripped_for_scope_with_block_exits(
        tenant_id="tenant-1", account_id="A1",
    )
    assert tripped is True
    assert block_exits is False


def test_is_tripped_for_scope_with_block_exits_hard():
    ksm = KillSwitchManager()
    ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "hard", actor="op",
        block_exits=True,
    )
    tripped, block_exits = ksm.is_tripped_for_scope_with_block_exits(
        tenant_id="tenant-1", account_id="A1",
    )
    assert tripped is True
    assert block_exits is True


def test_is_tripped_for_scope_with_block_exits_inactive():
    ksm = KillSwitchManager()
    tripped, block_exits = ksm.is_tripped_for_scope_with_block_exits(
        tenant_id="tenant-1", account_id="A1",
    )
    assert tripped is False
    assert block_exits is False


def test_block_exits_at_account_scope_only_overrides_global_soft():
    """If a HARD trip exists at ACCOUNT scope, it must propagate via the
    'any matching record' rule even when GLOBAL is a SOFT trip."""
    ksm = KillSwitchManager()
    ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", "soft-global", actor="op")
    ksm.trip(
        KillSwitchScope.ACCOUNT, "A1", "hard-account", actor="op",
        block_exits=True,
    )
    tripped, block_exits = ksm.is_tripped_for_scope_with_block_exits(
        tenant_id="tenant-1", account_id="A1",
    )
    assert tripped is True
    assert block_exits is True


def test_persistence_roundtrip_preserves_block_exits():
    ksm = KillSwitchManager()
    ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "hard", actor="op",
        block_exits=True,
    )
    rows = ksm.to_persistence_dict()
    assert any(row.get("block_exits") is True for row in rows)

    restored = KillSwitchManager.from_persistence_dict(rows)
    record = restored.get_record(KillSwitchScope.GLOBAL, "GLOBAL")
    assert record is not None
    assert record.block_exits is True


def test_from_persistence_dict_tolerates_pre_migration_rows():
    """Rows persisted before migration 017 do not have a block_exits key.
    Restoration must default to False without raising."""
    pre_migration_row = {
        "id": "rec-old",
        "scope": "GLOBAL",
        "scope_id": "GLOBAL",
        "state": "TRIPPED",
        "tripped_at": "2026-05-08T13:00:00Z",
        "tripped_by": "legacy",
        "trip_reason": "legacy auto-trip",
        # NB: no "block_exits" key.
    }
    restored = KillSwitchManager.from_persistence_dict([pre_migration_row])
    record = restored.get_record(KillSwitchScope.GLOBAL, "GLOBAL")
    assert record is not None
    assert record.state is KillSwitchState.TRIPPED
    assert record.block_exits is False
