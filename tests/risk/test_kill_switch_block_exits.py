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


# ---------------------------------------------------------------------------
# PR #233 review (Codex P1): SOFT→HARD upgrade + pre-migration tolerance.
# ---------------------------------------------------------------------------


def test_set_block_exits_upgrades_soft_trip_to_hard():
    """Codex P1: an operator must be able to escalate a SOFT auto-trip
    (e.g. legacy daily-loss propagation with block_exits=False) to a HARD
    panic stop without first clearing/rearming the kill switch."""
    ksm = KillSwitchManager()
    ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "auto-soft", actor="risk_manager_auto",
    )
    record = ksm.get_record(KillSwitchScope.GLOBAL, "GLOBAL")
    assert record.block_exits is False

    upgraded = ksm.set_block_exits(
        KillSwitchScope.GLOBAL, "GLOBAL",
        block_exits=True,
        actor="op-panic",
        reason="market crashing — stop all orders",
    )
    assert upgraded.block_exits is True
    assert upgraded.state is KillSwitchState.TRIPPED, (
        "set_block_exits must preserve the underlying state; the trip itself "
        "is unaffected"
    )


def test_set_block_exits_can_downgrade_hard_to_soft():
    """An operator may also want to downgrade HARD → SOFT (e.g. once
    panic conditions clear, allow trailing-lock to flatten)."""
    ksm = KillSwitchManager()
    ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "panic", actor="op",
        block_exits=True,
    )
    downgraded = ksm.set_block_exits(
        KillSwitchScope.GLOBAL, "GLOBAL",
        block_exits=False, actor="op", reason="allow trailing-lock to exit",
    )
    assert downgraded.block_exits is False


def test_set_block_exits_rejects_inactive_record():
    """No record exists yet → KeyError, not a silent no-op."""
    ksm = KillSwitchManager()
    try:
        ksm.set_block_exits(
            KillSwitchScope.GLOBAL, "GLOBAL",
            block_exits=True, actor="op", reason="x",
        )
    except KeyError:
        return
    raise AssertionError("set_block_exits must raise KeyError when no record")


def test_set_block_exits_rejects_cleared_state():
    """A record in CLEARED state cannot have block_exits flipped — the
    operator must rearm/trip first."""
    from app.risk.kill_switch import KillSwitchClearRequest, KillSwitchClearValidation

    ksm = KillSwitchManager()
    ksm.trip(KillSwitchScope.GLOBAL, "GLOBAL", "soft", actor="op")
    # TRIPPED → CLEAR_PENDING → CLEARED
    clear_req = KillSwitchClearRequest(
        scope=KillSwitchScope.GLOBAL, scope_id="GLOBAL",
        actor="op", reason_code="loss_resolved", request_id="req-1",
    )
    ksm.request_clear(
        clear_req,
        validation_fn=lambda _: KillSwitchClearValidation(passed=True),
    )
    ksm.confirm_clear(KillSwitchScope.GLOBAL, "GLOBAL")

    try:
        ksm.set_block_exits(
            KillSwitchScope.GLOBAL, "GLOBAL",
            block_exits=True, actor="op", reason="x",
        )
    except ValueError:
        return
    raise AssertionError(
        "set_block_exits must raise ValueError on CLEARED state"
    )


# --- Pre-migration schema tolerance (Codex P1 — save_state, load_state)


class _StubCursor:
    def __init__(self, parent):
        self.parent = parent
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql
        # Simulate information_schema column-presence check.
        if "information_schema.columns" in sql:
            self._iss_result = (
                [(1,)] if self.parent.has_block_exits_col else []
            )
            return
        # Track INSERTs / UPDATEs so the test can assert the right SQL
        # variant was used.
        self.parent.executed_sqls.append(sql)
        self.parent.executed_params.append(params)

    def fetchone(self):
        return self._iss_result[0] if self._iss_result else None

    def fetchall(self):
        return self.parent.fetchall_result


class _StubConn:
    def __init__(self, *, has_block_exits_col: bool):
        self.has_block_exits_col = has_block_exits_col
        self.executed_sqls: list = []
        self.executed_params: list = []
        self.fetchall_result: list = []

    def cursor(self):
        return _StubCursor(self)


def _clear_schema_cache():
    from app.risk import kill_switch as ks_mod
    ks_mod._SCHEMA_DETECTION_CACHE.clear()


def test_save_state_uses_legacy_sql_when_block_exits_column_absent(caplog):
    """Codex P1: pre-migration-017 schema must not break save_state. The
    legacy SQL (without block_exits) must be used and a WARNING emitted
    so the operational gap is observable."""
    import logging
    caplog.set_level(logging.WARNING)
    _clear_schema_cache()
    ksm = KillSwitchManager()
    ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "panic", actor="op",
        block_exits=True,
    )
    conn = _StubConn(has_block_exits_col=False)
    ksm.save_state(conn)

    assert conn.executed_sqls, "save_state must execute at least one INSERT"
    last_sql = conn.executed_sqls[-1]
    assert "block_exits" not in last_sql, (
        "legacy SQL must omit block_exits column when missing"
    )
    assert any(
        "block_exits column missing" in rec.getMessage()
        for rec in caplog.records
    ), "save_state must log WARNING when running on pre-migration schema"


def test_save_state_uses_modern_sql_when_block_exits_column_present():
    """Modern SQL (including block_exits) is used after migration."""
    _clear_schema_cache()
    ksm = KillSwitchManager()
    ksm.trip(
        KillSwitchScope.GLOBAL, "GLOBAL", "panic", actor="op",
        block_exits=True,
    )
    conn = _StubConn(has_block_exits_col=True)
    ksm.save_state(conn)

    assert conn.executed_sqls
    last_sql = conn.executed_sqls[-1]
    assert "block_exits" in last_sql, (
        "modern SQL must include block_exits when column exists"
    )
