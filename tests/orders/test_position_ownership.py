from __future__ import annotations

import pytest

from app.brokers.base import OrderRequest, OrderSide, OrderType, ProductType, TimeInForce
from app.core.operating_mode import OperatingMode
from app.orders.position_ownership import (
    ContractKey,
    OwnershipAuthorityViolation,
    OwnershipPersistenceBackend,
    PositionOwnershipStore,
    UNKNOWN_OWNER,
    UNKNOWN_STRATEGY_ID,
    derive_contract_key,
    derive_ownership_key,
)
from app.orders.ownership_state import OwnershipState


class _MemoryPersistenceBackend(OwnershipPersistenceBackend):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str, str, str, str, str, str], int] = {}
        self._authority_by_contract: dict[tuple[str, str, str, str, str, str, str], str] = {}

    def load_account_entries(
        self,
        *,
        tenant_id,
        broker_account_id,
    ):
        entries = {}
        for key, qty in self._rows.items():
            if int(qty or 0) == 0:
                continue
            (
                tenant,
                account,
                underlying,
                expiry,
                strike,
                option_right,
                product_type,
                strategy_id,
            ) = key
            if tenant != str(tenant_id) or account != str(broker_account_id):
                continue
            ckey = (underlying, expiry, strike, option_right, product_type)
            entry = entries.get(ckey)
            if entry is None:
                entry = type("Entry", (), {})()
                entry.pending_by_strategy = {}
                entry.net_by_strategy = {}
                entry.unknown_net_qty = 0
                entry.authority_path = self._authority_by_contract.get(
                    (
                        tenant,
                        account,
                        underlying,
                        expiry,
                        strike,
                        option_right,
                        product_type,
                    ),
                    "",
                )
                entries[ckey] = entry
            if strategy_id == UNKNOWN_STRATEGY_ID:
                entry.unknown_net_qty += int(qty)
            else:
                entry.net_by_strategy[strategy_id] = (
                    int(entry.net_by_strategy.get(strategy_id, 0)) + int(qty)
                )
        return entries

    def save_contract_state(
        self,
        *,
        tenant_id,
        broker_account_id,
        contract_key: ContractKey,
        entry,
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
        delete_keys = [row_key for row_key in self._rows if row_key[:7] == base]
        for row_key in delete_keys:
            self._rows.pop(row_key, None)
        self._authority_by_contract.pop(base, None)
        if entry is None:
            return
        self._authority_by_contract[base] = str(getattr(entry, "authority_path", "") or "")
        for strategy_id, qty in (entry.net_by_strategy or {}).items():
            net_qty = int(qty or 0)
            if net_qty == 0:
                continue
            self._rows[(*base, str(strategy_id))] = net_qty
        unknown_qty = int(getattr(entry, "unknown_net_qty", 0) or 0)
        if unknown_qty != 0:
            self._rows[(*base, UNKNOWN_STRATEGY_ID)] = unknown_qty


def _order_req(*, side: OrderSide) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=side,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
    )


def _contract_key() -> ContractKey:
    return ContractKey(
        underlying="NIFTY",
        expiry="2026-02-17",
        strike="25750",
        option_right="CE",
        product_type="INTRADAY",
    )


def test_derive_contract_key_populates_broker_token_and_instrument_version():
    contract_key, reason = derive_contract_key(
        _order_req(side=OrderSide.BUY),
        meta_resolver=lambda **_: {
            "symbol": "NIFTY17FEB2625750CE",
            "token": "12345",
            "exchange": "NFO",
            "underlying": "NIFTY",
            "expiry": "2026-02-17",
            "strike": "25750",
            "kind": "ATM_CE",
        },
    )

    assert reason is None
    assert contract_key is not None
    assert contract_key.broker_token == "12345"
    assert contract_key.exchange == "NFO"
    assert contract_key.broker_symbol == "NIFTY17FEB2625750CE"
    assert contract_key.instrument_version


def test_derive_contract_key_changes_instrument_version_when_token_changes():
    req = _order_req(side=OrderSide.BUY)
    req.symbol_token = None
    first_contract_key, _ = derive_contract_key(
        req,
        meta_resolver=lambda **_: {
            "symbol": "NIFTY17FEB2625750CE",
            "token": "12345",
            "underlying": "NIFTY",
            "expiry": "2026-02-17",
            "strike": "25750",
            "kind": "ATM_CE",
        },
    )
    second_contract_key, _ = derive_contract_key(
        req,
        meta_resolver=lambda **_: {
            "symbol": "NIFTY17FEB2625750CE",
            "token": "67890",
            "underlying": "NIFTY",
            "expiry": "2026-02-17",
            "strike": "25750",
            "kind": "ATM_CE",
        },
    )

    assert first_contract_key is not None
    assert second_contract_key is not None
    assert first_contract_key.as_storage_key() == second_contract_key.as_storage_key()
    assert first_contract_key.broker_token == "12345"
    assert second_contract_key.broker_token == "67890"
    assert first_contract_key.instrument_version != second_contract_key.instrument_version


def test_derive_contract_key_parses_numeric_expiry_option_symbols():
    request = OrderRequest(
        symbol="SENSEX2640274000CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="864957",
    )

    contract_key, reason = derive_contract_key(
        request,
        meta_resolver=lambda **_: None,
    )

    assert reason is None
    assert contract_key is not None
    assert contract_key.underlying == "SENSEX"
    assert contract_key.expiry == "2026-04-02"
    assert contract_key.strike == "74000"
    assert contract_key.option_right == "CE"


def test_derive_contract_key_parses_two_digit_numeric_month_option_symbols():
    request = OrderRequest(
        symbol="SENSEX26110274000CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="864957",
    )

    contract_key, reason = derive_contract_key(
        request,
        meta_resolver=lambda **_: None,
    )

    assert reason is None
    assert contract_key is not None
    assert contract_key.expiry == "2026-11-02"
    assert contract_key.strike == "74000"


def test_position_ownership_persistence_survives_restart_roundtrip():
    backend = _MemoryPersistenceBackend()
    contract_key = _contract_key()

    store1 = PositionOwnershipStore(backend=backend)
    decision = store1.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    assert decision.allowed is True
    store1.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        signed_qty=-75,
    )

    store2 = PositionOwnershipStore(backend=backend)
    assert (
        store2.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        )
        == "s1"
    )

    decision_exit = store2.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert decision_exit.allowed is True
    store2.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        signed_qty=75,
    )

    store3 = PositionOwnershipStore(backend=backend)
    assert (
        store3.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        )
        is None
    )


def test_position_ownership_blocks_cross_authority_mutation_after_reload():
    backend = _MemoryPersistenceBackend()
    contract_key = _contract_key()
    hub_store = PositionOwnershipStore(
        backend=backend,
        operating_mode=OperatingMode.HUB_AUTHORITATIVE,
    )
    legacy_store = PositionOwnershipStore(
        backend=backend,
        operating_mode=OperatingMode.LEGACY_AUTHORITATIVE,
    )

    decision = hub_store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )

    assert decision.allowed is True
    hub_store.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        signed_qty=-75,
    )
    assert (
        legacy_store.get_ownership_record(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        ).authority_path
        == "hub"
    )
    with pytest.raises(
        OwnershipAuthorityViolation,
        match="existing=hub requested=legacy action=try_acquire",
    ):
        legacy_store.try_acquire(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
            strategy_id="s2",
            is_exit_order=False,
            unknown_mode="block_entries",
        )


def test_position_ownership_reconcile_converts_mismatch_to_unknown_and_allows_exit_only():
    backend = _MemoryPersistenceBackend()
    contract_key = _contract_key()
    store = PositionOwnershipStore(backend=backend)

    store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    store.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        signed_qty=-75,
    )

    result = store.reconcile_broker_positions(
        tenant_id="t1",
        broker_account_id="a1",
        positions=[
            {
                "symbol": "NIFTY17FEB2625750CE",
                "quantity": -150,
                "product_type": ProductType.INTRADAY,
                "symbol_token": "12345",
            }
        ],
    )
    assert result.converted_to_unknown == 1
    assert (
        store.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        )
        == UNKNOWN_OWNER
    )

    blocked = store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s2",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    assert blocked.allowed is False

    allowed_exit = store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s2",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert allowed_exit.allowed is True

    store.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s2",
        signed_qty=150,
    )
    assert (
        store.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        )
        is None
    )


def test_position_ownership_reconcile_removes_stale_internal_contract_when_broker_flat():
    """Regression #69: destructive cleanup requires 2 consecutive zero-position polls.

    A single empty snapshot must NOT delete ownership state (corroboration guard).
    The second consecutive zero-poll is what triggers the removal.
    """
    backend = _MemoryPersistenceBackend()
    contract_key = _contract_key()
    store = PositionOwnershipStore(backend=backend)

    store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    store.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        signed_qty=-75,
    )

    # First zero-position poll: must be deferred (count=1 < 2).
    result1 = store.reconcile_broker_positions(
        tenant_id="t1",
        broker_account_id="a1",
        positions=[],
    )
    assert result1.removed_stale == 0, (
        "Single zero-poll must not delete ownership state (corroboration required)"
    )
    assert store.get_owner(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
    ) is not None, "Ownership must be preserved after first zero-poll"

    # Second consecutive zero-position poll: now count=2 >= 2, deletion proceeds.
    result2 = store.reconcile_broker_positions(
        tenant_id="t1",
        broker_account_id="a1",
        positions=[],
    )
    assert result2.removed_stale == 1, (
        "Second consecutive zero-poll must remove stale ownership entry"
    )
    assert (
        store.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        )
        is None
    )


def test_position_ownership_reconcile_handles_numeric_expiry_option_symbols():
    backend = _MemoryPersistenceBackend()
    store = PositionOwnershipStore(backend=backend)

    result = store.reconcile_broker_positions(
        tenant_id="t1",
        broker_account_id="a1",
        positions=[
            {
                "symbol": "SENSEX2640274000CE",
                "quantity": 20,
                "product_type": ProductType.INTRADAY,
                "symbol_token": "864957",
            }
        ],
    )

    assert result.parse_errors == 0
    assert result.added_unknown == 1
    assert (
        store.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=ContractKey(
                underlying="SENSEX",
                expiry="2026-04-02",
                strike="74000",
                option_right="CE",
                product_type="INTRADAY",
            ),
        )
        == UNKNOWN_OWNER
    )


def test_derive_ownership_key_normalizes_live_scope():
    ownership_key = derive_ownership_key(
        tenant_id="tenant-1",
        broker_account_id="broker-1",
        account_id="account-1",
        contract_key=_contract_key(),
    )

    assert ownership_key.tenant_id == "tenant-1"
    assert ownership_key.account_id == "account-1"
    assert ownership_key.broker_account_id == "broker-1"
    assert ownership_key.product_type == "INTRADAY"
    assert ownership_key.normalized_contract_key == (
        "NIFTY",
        "2026-02-17",
        "25750",
        "CE",
        "INTRADAY",
    )


def test_position_ownership_state_machine_tracks_pending_owned_and_releasing():
    store = PositionOwnershipStore(backend=_MemoryPersistenceBackend())
    contract_key = _contract_key()

    decision = store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    assert decision.allowed is True

    pending_record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
    )
    assert pending_record is not None
    assert pending_record.state == OwnershipState.PENDING_LOCK

    store.observe_fill_progress(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        filled_qty=1,
    )
    owned_record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
    )
    assert owned_record is not None
    assert owned_record.state == OwnershipState.OWNED

    exit_decision = store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    assert exit_decision.allowed is True

    releasing_record = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
    )
    assert releasing_record is not None
    assert releasing_record.state == OwnershipState.RELEASING


def test_reconciling_zero_poll_cleanup_uses_extra_confirmation():
    """Issue #318: RECONCILING ownership records get one extra zero-position
    confirmation, but broker-flat evidence must not be deferred forever.

    A record in RECONCILING state (set during startup recovery marking) needs
    additional corroboration before destructive cleanup is allowed.
    """
    from app.orders.position_ownership import OwnershipState

    backend = _MemoryPersistenceBackend()
    contract_key = _contract_key()
    store = PositionOwnershipStore(backend=backend)

    store.try_acquire(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )
    store.apply_fill(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
        strategy_id="s1",
        signed_qty=-50,
    )

    # Simulate startup recovery: mark ownership records as RECONCILING.
    store.mark_all_recovery_pending()

    # Confirm the record is now in RECONCILING state.
    rec = store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="a1",
        contract_key=contract_key,
    )
    assert rec is not None
    assert rec.state == OwnershipState.RECONCILING

    # Two zero-polls are not enough for a RECONCILING record.
    result1 = store.reconcile_broker_positions(
        tenant_id="t1", broker_account_id="a1", positions=[]
    )
    result2 = store.reconcile_broker_positions(
        tenant_id="t1", broker_account_id="a1", positions=[]
    )
    assert result1.removed_stale == 0
    assert result2.removed_stale == 0, (
        "RECONCILING record needs one extra zero-position confirmation"
    )

    # The third consecutive zero-poll confirms broker flat and clears the row.
    result3 = store.reconcile_broker_positions(
        tenant_id="t1", broker_account_id="a1", positions=[]
    )
    assert result3.removed_stale == 1
    assert (
        store.get_owner(
            tenant_id="t1",
            broker_account_id="a1",
            contract_key=contract_key,
        )
        is None
    )
