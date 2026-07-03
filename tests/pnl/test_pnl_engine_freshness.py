import copy
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import SimulatedClock
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import PnLSnapshot, PnLSnapshotKey, TradeEvent


def _build_engine(monkeypatch, *, base_time: datetime) -> tuple[PnLEngine, SimulatedClock]:
    monkeypatch.setattr("app.pnl.pnl_engine.insert_daily_pnl_snapshot", lambda record: None)
    monkeypatch.setenv("PNL_FRESHNESS_THRESHOLD_SECONDS", "300")
    clock = SimulatedClock(base_time)
    return PnLEngine(clock=clock), clock


class _CopyingPnLStateStore:
    def __init__(
        self,
        snapshots: list[PnLSnapshot],
        *,
        stale_realized_by_strategy: dict[StrategyId, float] | None = None,
    ) -> None:
        self._snapshots = {self._key(snapshot): copy.deepcopy(snapshot) for snapshot in snapshots}
        self._stale_realized_by_strategy = stale_realized_by_strategy or {}
        self.full_upsert_count = 0
        self.mark_upsert_count = 0

    @staticmethod
    def _key(snapshot: PnLSnapshot) -> tuple[TenantId, BrokerAccountId, StrategyId]:
        return (
            snapshot.key.tenant_id,
            snapshot.key.broker_account_id,
            snapshot.key.strategy_id,
        )

    @staticmethod
    def _parts_key(
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> tuple[TenantId, BrokerAccountId, StrategyId]:
        return (tenant_id, broker_account_id, strategy_id)

    def get_snapshot(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> PnLSnapshot | None:
        snapshot = self._snapshots.get(self._parts_key(tenant_id, broker_account_id, strategy_id))
        return copy.deepcopy(snapshot) if snapshot is not None else None

    def upsert_snapshot(self, snapshot: PnLSnapshot) -> None:
        self.full_upsert_count += 1
        self._snapshots[self._key(snapshot)] = copy.deepcopy(snapshot)

    def upsert_mark_snapshot(self, snapshot: PnLSnapshot) -> None:
        self.mark_upsert_count += 1
        key = self._key(snapshot)
        existing = self._snapshots.get(key)
        stored = copy.deepcopy(snapshot)
        if existing is not None and existing.session_date == snapshot.session_date:
            stored.realized_pnl = existing.realized_pnl
            snapshot.realized_pnl = existing.realized_pnl
        self._snapshots[key] = stored

    def list_account_snapshots(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
    ) -> list[PnLSnapshot]:
        snapshots: list[PnLSnapshot] = []
        for (t_id, b_id, strategy_id), snapshot in self._snapshots.items():
            if t_id != tenant_id or b_id != broker_account_id:
                continue
            copied = copy.deepcopy(snapshot)
            if strategy_id in self._stale_realized_by_strategy:
                copied.realized_pnl = self._stale_realized_by_strategy[strategy_id]
            snapshots.append(copied)
        return snapshots

    def stored_snapshot(
        self,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
    ) -> PnLSnapshot:
        snapshot = self._snapshots[self._parts_key(tenant_id, broker_account_id, strategy_id)]
        return copy.deepcopy(snapshot)


def test_realized_pnl_read_does_not_log_stale_mark_warning(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="NIFTY",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=5.0,
        )
    )
    clock.advance(timedelta(minutes=10))

    with caplog.at_level(logging.WARNING):
        realized = engine.get_current_realized_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

    assert realized == pytest.approx(-105.0)
    assert "realized PnL read" not in caplog.text


def test_total_pnl_read_still_warns_when_mark_snapshot_is_stale(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.update_unrealized_and_exposure(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        unrealized_pnl=125.0,
        gross_exposure=5000.0,
        as_of=base_time,
    )
    clock.advance(timedelta(minutes=10))

    with caplog.at_level(logging.WARNING):
        total = engine.get_current_total_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

    assert total == pytest.approx(125.0)
    assert "PnL snapshot stale during total PnL read" in caplog.text
    assert "source=mark_update" in caplog.text


def test_broker_sync_refreshes_total_pnl_freshness_for_existing_strategy(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="BANKNIFTY30MAR2653700CE",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=5.0,
        )
    )
    clock.advance(timedelta(minutes=10))
    engine.sync_account_mark_to_market(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        account_unrealized_pnl=0.0,
        account_gross_exposure=0.0,
        per_strategy_marks={},
        as_of=clock.now_utc(),
        source="broker_sync",
    )

    with caplog.at_level(logging.WARNING):
        total = engine.get_current_total_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

    snapshot = engine.get_snapshot(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
    )
    assert total == pytest.approx(-105.0)
    assert snapshot is not None
    assert snapshot.unrealized_pnl == pytest.approx(0.0)
    assert snapshot.freshness_source == "broker_sync"
    assert "PnL snapshot stale during total PnL read" not in caplog.text


def test_broker_sync_preserves_realized_when_snapshot_copy_is_stale(monkeypatch):
    base_time = datetime(2026, 7, 3, 8, 6, tzinfo=timezone.utc)
    monkeypatch.setattr("app.pnl.pnl_engine.insert_daily_pnl_snapshot", lambda record: None)
    monkeypatch.setattr(
        "app.pnl.pnl_engine.get_settings",
        lambda: type("S", (), {"default_time_zone": "UTC"})(),
    )
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("__external__")
    store = _CopyingPnLStateStore(
        [
            PnLSnapshot(
                key=PnLSnapshotKey(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                ),
                realized_pnl=1956.50,
                unrealized_pnl=0.0,
                gross_exposure=0.0,
                as_of=base_time,
                session_date=base_time.date(),
                freshness_updated_at=base_time,
                freshness_source="trade",
            )
        ],
        stale_realized_by_strategy={strategy_id: 5200.0},
    )
    engine = PnLEngine(state_store=store, clock=SimulatedClock(base_time))

    engine.sync_account_mark_to_market(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        account_unrealized_pnl=0.0,
        account_gross_exposure=0.0,
        per_strategy_marks={},
        as_of=base_time + timedelta(seconds=10),
        source="broker_sync",
    )

    stored = store.stored_snapshot(tenant_id, broker_account_id, strategy_id)
    assert stored.realized_pnl == pytest.approx(1956.50)
    assert stored.freshness_source == "broker_sync"
    assert store.mark_upsert_count >= 1


def test_display_realized_read_does_not_rewrite_same_session_snapshot(monkeypatch):
    base_time = datetime(2026, 7, 3, 8, 6, tzinfo=timezone.utc)
    monkeypatch.setattr("app.pnl.pnl_engine.insert_daily_pnl_snapshot", lambda record: None)
    monkeypatch.setattr(
        "app.pnl.pnl_engine.get_settings",
        lambda: type("S", (), {"default_time_zone": "UTC"})(),
    )
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("__external__")
    store = _CopyingPnLStateStore(
        [
            PnLSnapshot(
                key=PnLSnapshotKey(
                    tenant_id=tenant_id,
                    broker_account_id=broker_account_id,
                    strategy_id=strategy_id,
                ),
                realized_pnl=1956.50,
                unrealized_pnl=0.0,
                gross_exposure=0.0,
                as_of=base_time,
                session_date=base_time.date(),
                freshness_updated_at=base_time,
                freshness_source="trade",
            )
        ],
        stale_realized_by_strategy={strategy_id: 5200.0},
    )
    engine = PnLEngine(state_store=store, clock=SimulatedClock(base_time))

    engine.get_display_realized_pnl_account(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    )

    stored = store.stored_snapshot(tenant_id, broker_account_id, strategy_id)
    assert stored.realized_pnl == pytest.approx(1956.50)
    assert store.full_upsert_count == 0


def test_account_total_uses_seed_snapshot_without_double_counting_strategy_marks(
    monkeypatch,
):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, _clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="BANKNIFTY30MAR2653700CE",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=5.0,
        )
    )
    engine.sync_account_mark_to_market(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        account_unrealized_pnl=40.0,
        account_gross_exposure=240.0,
        per_strategy_marks={strategy_id: (40.0, 240.0)},
        as_of=base_time,
        source="broker_sync",
    )

    total = engine.get_current_total_pnl(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    )

    assert total == pytest.approx(-65.0)


def test_account_total_returns_none_for_broker_sync_stale_mark(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, _clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")

    engine.sync_account_mark_to_market(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        account_unrealized_pnl=0.0,
        account_gross_exposure=9150.0,
        per_strategy_marks={},
        as_of=base_time,
        source="broker_sync_stale_mark",
    )

    with caplog.at_level(logging.WARNING):
        total = engine.get_current_total_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )

    assert total is None
    assert "PnL snapshot unavailable during total PnL read" in caplog.text
    assert "source=broker_sync_stale_mark" in caplog.text


def test_display_realized_account_resets_previous_session(monkeypatch):
    base_time = datetime(2026, 5, 8, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("position_trailing_lock")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="NATURALGAS22MAY26265CE",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=0.0,
        )
    )
    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="NATURALGAS22MAY26265CE",
            qty=-1,
            price=120.0,
            trade_time=base_time,
            fees=0.0,
        )
    )

    assert engine.get_display_realized_pnl_account(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    ) == pytest.approx(20.0)

    clock.advance(timedelta(days=1))

    assert engine.get_display_realized_pnl_account(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    ) == pytest.approx(0.0)
    snap = engine.get_snapshot(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
    )
    assert snap is not None
    assert snap.realized_pnl == pytest.approx(0.0)
