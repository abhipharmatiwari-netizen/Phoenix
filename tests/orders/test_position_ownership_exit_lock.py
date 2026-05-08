"""Regression tests for issue #200: OWNED -> RELEASING exclusive exit lock.

The 2026-05-07 A1 NG flip-fill incident root-cause was the position ownership
store happily granting a SECOND pending exit-lock acquire on a contract whose
record was already in RELEASING state from an in-flight first exit. Two SL
exit orders fired ~11s apart on the same NATURALGAS22MAY26255CE 1-lot SHORT
(broker_order_ids 260507001449079 + 260507001449159), each writing a fresh
"position_ownership_pending_rows_persisted ... rows=1" line.

These tests pin the new behaviour: a second exit attempt against a contract
already in RELEASING is rejected with reason "exit_already_in_flight" until
either (a) the lock auto-releases via terminal fill / partial-fill handoff,
or (b) the watchdog window (default 90s, configurable via
POSITION_OWNERSHIP_EXIT_LOCK_MAX_SECONDS) expires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.orders.ownership_state import OwnershipState
from app.orders.position_ownership import (
    ContractKey,
    OwnershipPersistenceBackend,
    PositionOwnershipStore,
    UNKNOWN_STRATEGY_ID,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class _MemoryBackend(OwnershipPersistenceBackend):
    """In-memory persistence backend, copied minimal from test_position_ownership."""

    def __init__(self) -> None:
        self._rows: dict = {}
        self._authority: dict = {}

    def load_account_entries(self, *, tenant_id, broker_account_id):
        entries: dict = {}
        for key, qty in self._rows.items():
            if int(qty or 0) == 0:
                continue
            (tenant, account, u, e, s, r, p, sid) = key
            if tenant != str(tenant_id) or account != str(broker_account_id):
                continue
            ckey = (u, e, s, r, p)
            entry = entries.get(ckey)
            if entry is None:
                entry = type("Entry", (), {})()
                entry.pending_by_strategy = {}
                entry.net_by_strategy = {}
                entry.unknown_net_qty = 0
                entry.authority_path = self._authority.get(
                    (tenant, account, u, e, s, r, p), ""
                )
                entries[ckey] = entry
            if sid == UNKNOWN_STRATEGY_ID:
                entry.unknown_net_qty += int(qty)
            else:
                entry.net_by_strategy[sid] = (
                    int(entry.net_by_strategy.get(sid, 0)) + int(qty)
                )
        return entries

    def save_contract_state(
        self, *, tenant_id, broker_account_id, contract_key, entry
    ) -> None:
        base = (
            str(tenant_id),
            str(broker_account_id),
            contract_key.underlying,
            contract_key.expiry,
            contract_key.strike,
            contract_key.option_right,
            contract_key.product_type,
        )
        for k in [k for k in self._rows if k[:7] == base]:
            self._rows.pop(k, None)
        self._authority.pop(base, None)
        if entry is None:
            return
        self._authority[base] = str(getattr(entry, "authority_path", "") or "")
        for sid, q in (entry.net_by_strategy or {}).items():
            if int(q or 0) == 0:
                continue
            self._rows[(*base, str(sid))] = int(q)
        u = int(getattr(entry, "unknown_net_qty", 0) or 0)
        if u != 0:
            self._rows[(*base, UNKNOWN_STRATEGY_ID)] = u


def _ng_contract() -> ContractKey:
    """Mirrors the 2026-05-07 A1 NG flip-fill incident contract."""
    return ContractKey(
        underlying="NG",
        expiry="2026-05-22",
        strike="255",
        option_right="CE",
        product_type="INTRADAY",
    )


def _bn_contract() -> ContractKey:
    """A second, distinct contract for cross-contract independence tests."""
    return ContractKey(
        underlying="BANKNIFTY",
        expiry="2026-05-29",
        strike="49000",
        option_right="CE",
        product_type="INTRADAY",
    )


def _open_short_position(
    store: PositionOwnershipStore,
    *,
    contract,
    qty: int = 1250,
    strategy: str = "ema20_strategy",
) -> None:
    """Drive the store from NONE -> PENDING_LOCK -> OWNED for a short position."""
    decision = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id=strategy,
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    assert decision.allowed is True
    store.apply_fill(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id=strategy,
        signed_qty=-int(qty),
    )
    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    assert rec.state == OwnershipState.OWNED


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_first_exit_acquires_releasing_lock_with_released_at_set():
    """Baseline: a first exit acquire on an OWNED record transitions to
    RELEASING and stamps released_at."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract)

    decision = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert decision.allowed is True
    assert decision.reason == "acquired"

    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    assert rec.state == OwnershipState.RELEASING
    assert rec.released_at is not None


def test_second_exit_rejected_while_releasing_window_active():
    """Issue #200 P0: a second exit attempt on the same contract while the
    record is in RELEASING (within watchdog window) MUST be refused with
    reason=exit_already_in_flight, and MUST NOT increment pending_by_strategy.
    """
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract)

    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert first.allowed is True

    # Second exit, ~11s later in production. The watchdog is 90s default,
    # so the lock is still active.
    second = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert second.allowed is False
    assert second.reason == "exit_already_in_flight"
    assert second.acquired_pending is False

    # Record state is still RELEASING (rejection did not mutate state).
    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    assert rec.state == OwnershipState.RELEASING


def test_lock_auto_releases_on_terminal_fill_then_re_acquire_allowed():
    """When the broker confirms terminal fill (apply_fill drops net to 0),
    the record clears -- a follow-up exit on a fresh position should be
    allowed."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract)

    # Exit #1 acquires the lock.
    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert first.allowed is True

    # Broker confirms the fill (1250 BUY closes the 1250 SHORT -> flat).
    store.apply_fill(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        signed_qty=+1250,
    )
    rec_after_fill = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    # Record was cleared (NONE state) when entry flattened.
    assert rec_after_fill is None or rec_after_fill.state == OwnershipState.NONE

    # New short opens, then a fresh exit -- acquire must succeed.
    _open_short_position(store, contract=contract)
    second = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert second.allowed is True
    assert second.reason == "acquired"


def test_lock_auto_releases_on_partial_fill_handoff_then_retry_allowed():
    """A partial exit fill drops the net but doesn't flatten. The
    OwnershipRecord exits RELEASING (handed off to OWNED via the sync
    on apply_fill). A retry exit must then be permitted."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract, qty=1250)

    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert first.allowed is True

    # Partial close: 500 BUY against the 1250 SHORT -> net_qty=-750.
    store.apply_fill(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        signed_qty=+500,
    )
    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    # After partial fill, sync infers OWNED from the still-non-zero entry net.
    # The InvalidTransition handoff in _apply_record_transition reroutes
    # RELEASING -> RECONCILING -> OWNED, and released_at is cleared.
    assert rec.state == OwnershipState.OWNED
    assert rec.released_at is None

    # Retry exit must succeed -- this is the partial-exit follow-up path.
    retry = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert retry.allowed is True
    assert retry.reason == "acquired"


def test_watchdog_releases_stale_lock_after_window_expires():
    """A stuck RELEASING lock past POSITION_OWNERSHIP_EXIT_LOCK_MAX_SECONDS
    must NOT permanently deadlock the position. The next exit attempt logs
    a WARNING and is allowed (so a retry can drive recovery). The lock then
    re-stamps released_at to the new acquire time."""
    # Very short watchdog so we don't actually have to wait.
    store = PositionOwnershipStore(
        backend=_MemoryBackend(),
        exit_lock_max_seconds=2.0,
    )
    contract = _ng_contract()
    _open_short_position(store, contract=contract)

    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert first.allowed is True

    # Reach into the live record (get_ownership_record returns a deepcopy).
    # The scope_key is constructed via the store's static helper so it
    # matches whatever derive_ownership_key produces.
    live_records = store._ownership_records
    assert len(live_records) == 1, "expected exactly one ownership record"
    scoped_key, live_rec = next(iter(live_records.items()))
    assert live_rec.state == OwnershipState.RELEASING
    original_released_at = live_rec.released_at
    assert original_released_at is not None

    # Simulate the watchdog window having expired by rolling released_at back.
    live_rec.released_at = original_released_at - timedelta(seconds=10)

    # Second exit: watchdog expired -> allowed (with WARNING in the logs).
    second = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert second.allowed is True
    assert second.reason == "acquired"

    # released_at was re-stamped to ~now (definitely later than the back-rolled
    # value we wrote in).
    live_rec_after = store._ownership_records[scoped_key]
    assert live_rec_after.state == OwnershipState.RELEASING
    assert live_rec_after.released_at is not None
    assert live_rec_after.released_at > (original_released_at - timedelta(seconds=10))


def test_different_contracts_remain_independent():
    """An exit lock on contract A must not block an exit on contract B,
    even on the same broker account / strategy."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    a = _ng_contract()
    b = _bn_contract()

    _open_short_position(store, contract=a)
    _open_short_position(store, contract=b)

    exit_a = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=a,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert exit_a.allowed is True

    exit_b = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=b,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert exit_b.allowed is True

    # Re-attempting the exit on A is still rejected.
    duplicate_a = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=a,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert duplicate_a.allowed is False
    assert duplicate_a.reason == "exit_already_in_flight"


def test_second_exit_from_different_strategy_also_blocked():
    """The lock is contract-level, not strategy-level. A second exit
    attempt from a *different* strategy on the same RELEASING contract
    must also be rejected (this is the broader ownership protection
    that covers exclusive_nifty_ce_buy / put_momentum_scalper /
    nifty_weekly_credit_spreads, not just ema20_strategy)."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract, strategy="ema20_strategy")

    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert first.allowed is True

    # A different strategy attempts to exit the same contract -- contract is
    # owned by ema20_strategy, so contract_locked applies even before our
    # exit_already_in_flight guard. Either rejection is acceptable; we just
    # require allowed=False.
    second = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="exclusive_nifty_ce_buy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert second.allowed is False
    assert second.reason in {"exit_already_in_flight", "contract_locked"}


def test_first_entry_unaffected_by_exit_lock_guard():
    """Sanity: the new guard is exit-only. Fresh entries on a clean record
    must not be impacted."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()

    decision = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    assert decision.allowed is True
    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    assert rec.state == OwnershipState.PENDING_LOCK
    # No exit acquired, so released_at must be None.
    assert rec.released_at is None


def test_2026_05_07_ng_incident_regression():
    """Direct regression for the 2026-05-07 A1 NG incident timeline:

      14:30:03  exit #1 acquire -> rows=1                  (ALLOWED)
      14:30:03  broker_order_id=260507001449079 -> SUCCESS
      14:30:14  exit #2 acquire -> bug: rows=1 again       (BUG)

    With this fix the second acquire returns
    PositionOwnershipDecision(allowed=False, reason="exit_already_in_flight")
    and the second broker order is never submitted.
    """
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract, qty=1250)

    # 14:30:03 -- first exit attempt.
    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert first.allowed is True
    assert first.acquired_pending is True

    # 14:30:14 -- second exit attempt (the buggy duplicate). Must be refused.
    second = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert second.allowed is False
    assert second.reason == "exit_already_in_flight"
    assert second.acquired_pending is False
