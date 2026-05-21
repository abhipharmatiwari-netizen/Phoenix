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

from datetime import timedelta

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


def test_exit_after_flattened_position_is_rejected():
    """Issue #317: stale strategy state must not submit a second broker exit
    after the first terminal fill already flattened Phoenix ownership.
    """
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

    store.apply_fill(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        signed_qty=+1250,
    )
    assert (
        store.get_owner(
            tenant_id="tenant-1",
            broker_account_id="A1",
            contract_key=contract,
        )
        is None
    )

    stale_duplicate = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert stale_duplicate.allowed is False
    assert stale_duplicate.reason == "no_owned_position_to_exit"


def test_lock_auto_releases_on_partial_fill_handoff_then_retry_allowed():
    """A partial exit fill drops the net but doesn't flatten. apply_fill
    decrements pending_by_strategy[strategy] from 1 to 0 (one fill = one
    decrement, regardless of partial vs full), which clears released_at
    explicitly. A retry exit on the residual quantity must then be
    permitted."""
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
    # released_at must be cleared once the strategy's pending count reaches
    # zero (the original exit-claim is no longer pending).
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


# ----------------------------------------------------------------------
# Codex review hardening (PR #202)
# Each test below pins a behaviour the Codex bot flagged as a P1 / P2
# correctness gap in the original state-based guard.
# ----------------------------------------------------------------------

def test_lock_persists_through_state_transition_to_owned():
    """Codex P1/P2: an entry-acquire (or any operation that drives a sync
    of the ownership record) re-infers OWNED from the still-non-zero entry
    net and used to clobber `released_at`. The lock now decouples from
    `state` -- as long as the owning strategy still has pending_by_strategy
    > 0, the next exit acquire is refused regardless of current state."""
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

    # Drive the record's state out of RELEASING by force-applying a
    # transition (simulating broker reconciliation putting the record
    # into RECONCILING / OWNED while the original exit is still pending).
    live = next(iter(store._ownership_records.values()))
    assert live.state == OwnershipState.RELEASING
    assert live.released_at is not None

    # Move the state but NOT released_at (the lock claim).
    live.state = OwnershipState.OWNED
    # released_at intentionally preserved -- it represents the in-flight
    # exit claim, not the current state.

    # A second exit attempt must still be refused -- the lock protects
    # the in-flight order, not just the RELEASING state.
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


def test_partial_fill_observation_refreshes_lock_watchdog():
    """Codex P2 #4: observe_fill_progress reaffirms RELEASING with a fresh
    'partial_exit_fill_observed' reason. The watchdog must be refreshed on
    that evidence -- otherwise an exit that takes longer than 90s but
    visibly receives partial fills would have its lock expire and admit a
    duplicate exit submission."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract)

    store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    live = next(iter(store._ownership_records.values()))
    original_released_at = live.released_at
    assert original_released_at is not None

    # Roll released_at back so the watchdog is "old".
    live.released_at = original_released_at - timedelta(seconds=5)
    aged = live.released_at

    # A partial-fill broker observation arrives.
    store.observe_fill_progress(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        filled_qty=500,
    )

    # released_at must be refreshed to ~now, NOT left at the aged value.
    refreshed = store._ownership_records[
        next(iter(store._ownership_records.keys()))
    ].released_at
    assert refreshed is not None
    assert refreshed > aged


def test_release_pending_clears_lock_when_pending_returns_to_zero():
    """Codex review: when an exit order is rejected/cancelled and
    OrderLifecycleService calls release_pending, the per-strategy pending
    count returns to zero and the lock must clear so a retry can proceed.
    """
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()
    _open_short_position(store, contract=contract)

    store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    live = next(iter(store._ownership_records.values()))
    assert live.released_at is not None

    # Broker rejects the exit; lifecycle calls release_pending.
    store.release_pending(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
    )

    # The lock must be released -- a retry must succeed.
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


def test_unknown_owner_exit_also_locks():
    """Codex P1 #3: the first exit on a broker-only / UNKNOWN-owner
    position lands in RECONCILING (not RELEASING). The state-based guard
    missed this entirely; the released_at-based guard must catch it too --
    a duplicate UNKNOWN-owner flatten must be refused."""
    store = PositionOwnershipStore(backend=_MemoryBackend())
    contract = _ng_contract()

    # Seed an UNKNOWN-owner entry directly (simulates broker reconcile of
    # a position opened outside Phoenix's option universe).
    scoped = (
        "tenant-1", "A1", "A1",
        contract.as_storage_key(),
    )
    from app.orders.position_ownership import _OwnershipEntry
    entry = _OwnershipEntry(unknown_net_qty=-1250, authority_path="hub")
    store._entries[scoped] = entry
    store._loaded_accounts.add(("tenant-1", "A1"))

    # First flatten attempt by a strategy -- allowed via UNKNOWN-owner
    # branch, lands the record in RECONCILING.
    first = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="allow_entries",
    )
    assert first.allowed is True
    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    # State is RECONCILING (UNKNOWN-owner exit), not RELEASING.
    assert rec.state == OwnershipState.RECONCILING
    # But released_at IS set so the duplicate guard fires.
    assert rec.released_at is not None

    # Duplicate flatten attempt -- must be refused.
    second = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="allow_entries",
    )
    assert second.allowed is False
    assert second.reason == "exit_already_in_flight"


def test_restart_with_pending_persisted_re_locks_with_fresh_watchdog():
    """Codex P1 #1 (partial mitigation): on process restart, in-memory
    `released_at` is lost. If persistence preserved a non-zero
    pending_by_strategy[*] count, the prior process had an exit in flight.
    _ensure_account_loaded must stamp released_at = now to give a fresh
    watchdog window during which duplicate exits are blocked, even though
    the original released_at is unrecoverable."""

    # Custom backend that returns an entry with both net and pending,
    # mimicking what PostgresPositionOwnershipBackend produces when
    # persist_pending_locks=True and the prior process had an in-flight
    # exit at the moment of crash/restart.
    class _PendingPersistedBackend(OwnershipPersistenceBackend):
        def __init__(self) -> None:
            self.saved: list = []

        def load_account_entries(self, *, tenant_id, broker_account_id):
            from app.orders.position_ownership import _OwnershipEntry
            entry = _OwnershipEntry(
                pending_by_strategy={"ema20_strategy": 1},
                net_by_strategy={"ema20_strategy": -1250},
                authority_path="hub",
            )
            return {_ng_contract().as_storage_key(): entry}

        def save_contract_state(self, **kw) -> None:
            self.saved.append(kw)

    contract = _ng_contract()
    store = PositionOwnershipStore(backend=_PendingPersistedBackend())

    # Trigger the load (any operation does this).
    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    # released_at must have been stamped on load because pending was
    # non-zero in persisted state.
    assert rec.released_at is not None

    # Duplicate exit attempt against the reloaded state must be refused.
    duplicate = store.try_acquire(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
        strategy_id="ema20_strategy",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert duplicate.allowed is False
    assert duplicate.reason == "exit_already_in_flight"


def test_restart_without_pending_does_not_pre_lock():
    """Counterpart to the restart test: if persistence has only net (no
    pending claim), the prior process had no exit in flight at restart
    time. released_at must NOT be stamped -- a clean exit acquire must
    succeed normally."""

    class _NetOnlyBackend(OwnershipPersistenceBackend):
        def load_account_entries(self, *, tenant_id, broker_account_id):
            from app.orders.position_ownership import _OwnershipEntry
            entry = _OwnershipEntry(
                pending_by_strategy={},
                net_by_strategy={"ema20_strategy": -1250},
                authority_path="hub",
            )
            return {_ng_contract().as_storage_key(): entry}

        def save_contract_state(self, **kw) -> None:
            return None

    contract = _ng_contract()
    store = PositionOwnershipStore(backend=_NetOnlyBackend())

    rec = store.get_ownership_record(
        tenant_id="tenant-1",
        broker_account_id="A1",
        contract_key=contract,
    )
    assert rec is not None
    # No pending in persistence -> no synthetic released_at on load.
    assert rec.released_at is None

    # Fresh exit acquire must succeed.
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
