from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.orders.position_ownership import (
    ContractKey,
    PostgresPositionOwnershipBackend,
    PositionOwnershipStore,
    _OwnershipEntry,
)


@dataclass
class _FakeConnection:
    backend: "_FakeBackend"

    def cursor(self) -> "_FakeCursor":
        return _FakeCursor(self.backend)

    def commit(self) -> None:
        self.backend.commits += 1

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeCursor:
    def __init__(self, backend: "_FakeBackend") -> None:
        self._backend = backend
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params=None) -> None:
        sql_upper = str(sql or "").strip().upper()
        payload = dict(params or {})
        if sql_upper.startswith("SELECT"):
            self._rows = [
                (
                    row["underlying"],
                    row["expiry"],
                    row["strike"],
                    row["option_right"],
                    row["product_type"],
                    row["strategy_id"],
                    row.get("authority_path", "hub"),
                    row["net_qty"],
                )
                for row in self._backend.rows
                if row["tenant_id"] == payload["tenant_id"]
                and row["broker_account_id"] == payload["broker_account_id"]
                and int(row["net_qty"] or 0) != 0
            ]
            return
        if sql_upper.startswith("DELETE"):
            self._backend.rows = [
                row
                for row in self._backend.rows
                if not (
                    row["tenant_id"] == payload["tenant_id"]
                    and row["broker_account_id"] == payload["broker_account_id"]
                    and row["underlying"] == payload["underlying"]
                    and row["expiry"] == payload["expiry"]
                    and row["strike"] == payload["strike"]
                    and row["option_right"] == payload["option_right"]
                    and row["product_type"] == payload["product_type"]
                )
            ]
            return
        if sql_upper.startswith("INSERT INTO"):
            row = dict(payload)
            row.setdefault("updated_at", datetime.now(timezone.utc))
            self._backend.rows.append(row)
            return

    def fetchall(self):
        return list(self._rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeBackend(PostgresPositionOwnershipBackend):
    def __init__(self, *, persist_pending_locks: bool) -> None:
        self.rows: list[dict[str, object]] = []
        self.commits = 0
        super().__init__(
            dsn="postgresql://unused",
            table_name="position_ownership_ledger",
            persist_pending_locks=persist_pending_locks,
        )

    def _ensure_schema(self) -> None:
        return

    def _conn(self):
        return _FakeConnection(self)


def _contract_key() -> ContractKey:
    return ContractKey(
        underlying="NIFTY",
        expiry="2026-03-26",
        strike="22000",
        option_right="CE",
        product_type="INTRADAY",
    )


def test_pending_rows_round_trip_with_net_and_unknown():
    backend = _FakeBackend(persist_pending_locks=True)
    contract_key = _contract_key()
    entry = _OwnershipEntry(
        pending_by_strategy={"strategy-a": 2},
        net_by_strategy={"strategy-a": 1},
        unknown_net_qty=3,
        authority_path="hub",
    )

    backend.save_contract_state(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        contract_key=contract_key,
        entry=entry,
    )

    loaded = backend.load_account_entries(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
    )

    assert loaded[contract_key.as_storage_key()].pending_by_strategy == {"strategy-a": 2}
    assert loaded[contract_key.as_storage_key()].net_by_strategy == {"strategy-a": 1}
    assert loaded[contract_key.as_storage_key()].unknown_net_qty == 3
    assert loaded[contract_key.as_storage_key()].authority_path == "hub"


def test_pending_lock_survives_reload_and_blocks_other_strategy():
    backend = _FakeBackend(persist_pending_locks=True)
    store = PositionOwnershipStore(backend=backend)
    contract_key = _contract_key()

    acquired = store.try_acquire(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        contract_key=contract_key,
        strategy_id=StrategyId("strategy-a"),
        is_exit_order=False,
        unknown_mode="block_entries",
    )

    reloaded_store = PositionOwnershipStore(backend=backend)
    blocked = reloaded_store.try_acquire(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        contract_key=contract_key,
        strategy_id=StrategyId("strategy-b"),
        is_exit_order=False,
        unknown_mode="block_entries",
    )

    assert acquired.allowed is True
    assert acquired.acquired_pending is True
    assert blocked.allowed is False
    assert blocked.owner == "strategy-a"
    assert blocked.reason == "contract_locked"


def test_zero_pending_counts_are_not_persisted():
    backend = _FakeBackend(persist_pending_locks=True)
    contract_key = _contract_key()
    entry = _OwnershipEntry(pending_by_strategy={"strategy-a": 0}, authority_path="hub")

    backend.save_contract_state(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        contract_key=contract_key,
        entry=entry,
    )

    assert backend.rows == []


def test_flag_off_keeps_net_only_persistence():
    backend = _FakeBackend(persist_pending_locks=False)
    contract_key = _contract_key()
    entry = _OwnershipEntry(
        pending_by_strategy={"strategy-a": 2},
        net_by_strategy={"strategy-a": 1},
        unknown_net_qty=3,
        authority_path="hub",
    )

    backend.save_contract_state(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        contract_key=contract_key,
        entry=entry,
    )
    loaded = backend.load_account_entries(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
    )

    assert loaded[contract_key.as_storage_key()].pending_by_strategy == {}
    assert loaded[contract_key.as_storage_key()].net_by_strategy == {"strategy-a": 1}
    assert loaded[contract_key.as_storage_key()].unknown_net_qty == 3
    assert loaded[contract_key.as_storage_key()].authority_path == "hub"
