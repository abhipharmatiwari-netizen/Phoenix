"""Tests for PositionOwnershipStore.resolve_orphan_review (Architecture S11.5)."""

from __future__ import annotations

import pytest

from app.orders.ownership_state import OwnershipState
from app.orders.position_ownership import (
    ContractKey,
    PositionOwnershipStore,
)


def _contract_key() -> ContractKey:
    return ContractKey(
        underlying="NIFTY",
        expiry="2026-02-17",
        strike="25750",
        option_right="CE",
        product_type="INTRADAY",
    )


def _make_orphan_review_store() -> PositionOwnershipStore:
    """Build a store with a single contract in ORPHAN_REVIEW state.

    We achieve this by simulating the conditions that lead to ORPHAN_REVIEW:
    reconciliation with ambiguous multi-strategy ownership (two strategies
    with non-zero net quantities).
    """
    store = PositionOwnershipStore()
    ck = _contract_key()
    tenant_id = "t1"
    broker_account_id = "a1"

    # Acquire + fill for strategy s1
    store.try_acquire(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        contract_key=ck,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    store.apply_fill(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        contract_key=ck,
        strategy_id="s1",
        signed_qty=1,
    )

    # Now force-inject a second strategy net ownership to create ambiguity.
    # The store's internal _entries dict will then have two net_by_strategy
    # entries, which _infer_record_target maps to ORPHAN_REVIEW.
    scoped = store._scope_key(tenant_id, broker_account_id, ck)
    with store._lock:
        entry = store._entries.get(scoped)
        assert entry is not None, "Entry should exist after fill"
        entry.net_by_strategy["s2"] = 1
        store._sync_ownership_record_from_entry(
            scoped_key=scoped,
            entry=entry,
            reason="test_force_ambiguous_ownership",
        )

    record = store.get_ownership_record(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        contract_key=ck,
    )
    assert record is not None
    assert record.state == OwnershipState.ORPHAN_REVIEW, (
        f"Expected ORPHAN_REVIEW but got {record.state.value}"
    )
    return store


# ---- ADOPT ----

def test_resolve_orphan_adopt_transitions_to_owned():
    store = _make_orphan_review_store()
    ck = _contract_key()

    result = store.resolve_orphan_review(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
        decision="ADOPT",
        actor="operator@test",
        reason="operator adopts the position",
        request_id="req-001",
    )

    assert result["status"] == "resolved"
    assert result["decision"] == "ADOPT"
    assert result["previous_state"] == "ORPHAN_REVIEW"
    assert result["new_state"] == "OWNED"

    record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
    )
    assert record is not None
    assert record.state == OwnershipState.OWNED
    assert record.owner_strategy_id == "operator@test"
    assert record.acquired_at is not None


# ---- FLATTEN ----

def test_resolve_orphan_flatten_transitions_to_none():
    store = _make_orphan_review_store()
    ck = _contract_key()

    result = store.resolve_orphan_review(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
        decision="FLATTEN",
        actor="operator@test",
        reason="flatten orphaned position",
        request_id="req-002",
    )

    assert result["status"] == "resolved"
    assert result["decision"] == "FLATTEN"
    assert result["previous_state"] == "ORPHAN_REVIEW"
    assert result["new_state"] == "NONE"

    record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
    )
    # Record is cleared after flatten
    assert record is None


# ---- SUPPRESS ----

def test_resolve_orphan_suppress_transitions_to_none():
    store = _make_orphan_review_store()
    ck = _contract_key()

    result = store.resolve_orphan_review(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
        decision="SUPPRESS",
        actor="operator@test",
        reason="suppress noise",
        request_id="req-003",
    )

    assert result["status"] == "resolved"
    assert result["decision"] == "SUPPRESS"
    assert result["previous_state"] == "ORPHAN_REVIEW"
    assert result["new_state"] == "NONE"

    record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
    )
    assert record is None


# ---- CONTINUE_OBSERVING ----

def test_resolve_orphan_continue_observing_stays_in_orphan_review():
    store = _make_orphan_review_store()
    ck = _contract_key()

    result = store.resolve_orphan_review(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
        decision="CONTINUE_OBSERVING",
        actor="operator@test",
        reason="wait for more evidence",
        request_id="req-004",
    )

    assert result["status"] == "resolved"
    assert result["decision"] == "CONTINUE_OBSERVING"
    assert result["previous_state"] == "ORPHAN_REVIEW"
    assert result["new_state"] == "ORPHAN_REVIEW"

    record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
    )
    assert record is not None
    assert record.state == OwnershipState.ORPHAN_REVIEW
    assert record.last_evidence_at is not None


# ---- Error cases ----

def test_resolve_orphan_invalid_decision_raises():
    store = _make_orphan_review_store()
    ck = _contract_key()

    with pytest.raises(ValueError, match="Invalid orphan review decision"):
        store.resolve_orphan_review(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=ck,
            decision="INVALID",
            actor="operator@test",
            reason="bad decision",
        )


def test_resolve_orphan_wrong_state_raises():
    """Trying to resolve a record not in ORPHAN_REVIEW should fail."""
    store = PositionOwnershipStore()
    ck = _contract_key()

    # Create an OWNED record
    store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    store.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
        strategy_id="s1",
        signed_qty=1,
    )

    record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=ck,
    )
    assert record is not None
    assert record.state == OwnershipState.OWNED

    with pytest.raises(ValueError, match="not ORPHAN_REVIEW"):
        store.resolve_orphan_review(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=ck,
            decision="ADOPT",
            actor="operator@test",
            reason="should fail",
        )


def test_resolve_orphan_no_record_raises():
    """Trying to resolve a non-existent record should fail."""
    store = PositionOwnershipStore()
    ck = _contract_key()

    with pytest.raises(ValueError, match="No ownership record found"):
        store.resolve_orphan_review(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=ck,
            decision="ADOPT",
            actor="operator@test",
            reason="should fail",
        )
