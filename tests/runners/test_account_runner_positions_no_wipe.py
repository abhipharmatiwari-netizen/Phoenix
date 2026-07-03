import asyncio
from types import SimpleNamespace

from app.brokers.base import Position, ProductType
from app.brokers.positions_types import PositionsFetchResult, PositionsStatus
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.data.state_store import StateStore
from app.hub.account_runner import (
    AccountRunner,
    _POSITIONS_BACKOFF,
    _POSITIONS_LOCKS,
    _POSITIONS_REFCOUNTS,
)
from app.tenants.models import BrokerAccountModel


class _BlockedBrokerClient:
    async def login(self):
        return None

    async def get_balance(self):
        raise NotImplementedError

    async def get_positions(self):
        return PositionsFetchResult(
            status=PositionsStatus.BLOCKED,
            positions=None,
            reason="waf_rejected",
            http_status=200,
            retry_after_seconds=30.0,
            raw_snippet="Request Rejected",
        )

    async def get_orders(self):
        return []

    async def place_order(self, request):
        raise NotImplementedError


class _OkBrokerClient:
    async def login(self):
        return None

    async def get_balance(self):
        raise NotImplementedError

    async def get_positions(self):
        return PositionsFetchResult(
            status=PositionsStatus.OK,
            positions=[
                Position(
                    symbol="TEST",
                    quantity=1,
                    avg_price=100.0,
                    product_type=ProductType.INTRADAY,
                )
            ],
            reason="ok",
        )

    async def get_orders(self):
        return []

    async def place_order(self, request):
        raise NotImplementedError


class _FlatBrokerClient:
    async def login(self):
        return None

    async def get_balance(self):
        raise NotImplementedError

    async def get_positions(self):
        return PositionsFetchResult(
            status=PositionsStatus.OK,
            positions=[],
            reason="ok",
        )

    async def get_orders(self):
        return []

    async def place_order(self, request):
        raise NotImplementedError


def _dummy_account() -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id=BrokerAccountId("ba_test"),
        tenant_id=TenantId("tenant_test"),
        broker_type="angel",
        display_name="test",
        client_code="test_client",
        secret_ref="secret/ref",
    )


def test_sync_positions_does_not_overwrite_store_when_blocked():
    state_store = StateStore()
    account = _dummy_account()
    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BlockedBrokerClient(),
        runtime_mode="LIVE",
        state_store=state_store,
        poll_interval_seconds=1,
    )
    initial_positions = [
        Position(
            symbol="TEST",
            quantity=1,
            avg_price=100.0,
            product_type=ProductType.INTRADAY,
        )
    ]
    state_store.set_positions(account.broker_account_id, initial_positions)

    asyncio.run(runner._sync_positions())

    stored_positions = state_store.get_positions(account.broker_account_id)
    assert len(stored_positions) == 1
    assert stored_positions[0].symbol == "TEST"
    assert stored_positions[0].quantity == 1
    asyncio.run(runner.stop())


def test_shared_positions_maps_are_cleaned_up_after_last_runner_stop(monkeypatch):
    monkeypatch.setenv("ACCOUNT_RUNNER_SHARED_POSITIONS_CLEANUP_ENABLED", "true")
    state_store = StateStore()
    account = BrokerAccountModel(
        broker_account_id=BrokerAccountId("ba_test_cleanup"),
        tenant_id=TenantId("tenant_test"),
        broker_type="angel",
        display_name="test-cleanup",
        client_code="test_cleanup_client",
        secret_ref="secret/ref",
    )
    runner_a = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BlockedBrokerClient(),
        runtime_mode="LIVE",
        state_store=state_store,
        poll_interval_seconds=1,
    )
    runner_b = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BlockedBrokerClient(),
        runtime_mode="LIVE",
        state_store=state_store,
        poll_interval_seconds=1,
    )

    key = runner_a._positions_key()
    assert _POSITIONS_REFCOUNTS[key] >= 2
    assert key in _POSITIONS_LOCKS
    assert key in _POSITIONS_BACKOFF

    asyncio.run(runner_a.stop())
    assert key in _POSITIONS_LOCKS
    assert _POSITIONS_REFCOUNTS.get(key, 0) >= 1

    asyncio.run(runner_b.stop())
    assert key not in _POSITIONS_REFCOUNTS
    assert key not in _POSITIONS_LOCKS
    assert key not in _POSITIONS_BACKOFF


def test_sync_positions_logs_when_ownership_converts_to_unknown(caplog):
    state_store = StateStore()
    account = _dummy_account()
    ownership_store = SimpleNamespace(
        reconcile_broker_positions=lambda **_kwargs: SimpleNamespace(
            changed=True,
            added_unknown=0,
            converted_to_unknown=1,
            removed_stale=0,
            parse_errors=0,
        )
    )
    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_OkBrokerClient(),
        runtime_mode="LIVE",
        state_store=state_store,
        position_ownership_store=ownership_store,
        poll_interval_seconds=1,
    )
    caplog.set_level("WARNING", logger="app.hub.account_runner")

    asyncio.run(runner._sync_positions(force=True))

    assert "event_type=OWNERSHIP_CONVERTED_TO_UNKNOWN" in caplog.text
    asyncio.run(runner.stop())


def test_sync_positions_refreshes_pnl_from_broker_positions(monkeypatch):
    state_store = StateStore()
    account = _dummy_account()
    captured = {}

    class _OwnedBrokerClient:
        async def login(self):
            return None

        async def get_balance(self):
            raise NotImplementedError

        async def get_positions(self):
            return PositionsFetchResult(
                status=PositionsStatus.OK,
                positions=[
                    Position(
                        symbol="BANKNIFTY30MAR2653700CE",
                        quantity=2,
                        avg_price=100.0,
                        product_type=ProductType.INTRADAY,
                    )
                ],
                reason="ok",
            )

        async def get_orders(self):
            return []

        async def place_order(self, request):
            raise NotImplementedError

    class _PnlEngine:
        def sync_account_mark_to_market(self, **kwargs):
            captured.update(kwargs)

    ownership_store = SimpleNamespace(
        reconcile_broker_positions=lambda **_kwargs: SimpleNamespace(
            changed=False,
            added_unknown=0,
            converted_to_unknown=0,
            removed_stale=0,
            parse_errors=0,
        ),
        get_owner=lambda **_kwargs: "ema20-trend",
    )
    monkeypatch.setattr(
        "app.hub.account_runner.dashboard_bus.get_last_price",
        lambda symbol: 120.0 if symbol == "BANKNIFTY30MAR2653700CE" else None,
    )

    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_OwnedBrokerClient(),
        runtime_mode="LIVE",
        state_store=state_store,
        pnl_engine=_PnlEngine(),
        position_ownership_store=ownership_store,
        poll_interval_seconds=1,
    )

    asyncio.run(runner._sync_positions(force=True))

    assert captured["tenant_id"] == account.tenant_id
    assert captured["broker_account_id"] == account.broker_account_id
    assert captured["account_unrealized_pnl"] == 40.0
    assert captured["account_gross_exposure"] == 240.0
    assert captured["per_strategy_marks"] == {StrategyId("ema20-trend"): (40.0, 240.0)}
    asyncio.run(runner.stop())


def test_sync_positions_resolves_ltp_from_instrument_metadata(monkeypatch):
    state_store = StateStore()
    account = _dummy_account()
    captured = {}

    class _BrokerClientWithBrokerSymbol:
        async def login(self):
            return None

        async def get_balance(self):
            raise NotImplementedError

        async def get_positions(self):
            return PositionsFetchResult(
                status=PositionsStatus.OK,
                positions=[
                    Position(
                        symbol="BANKNIFTY28JUL2658000CE",
                        quantity=2,
                        avg_price=100.0,
                        product_type=ProductType.INTRADAY,
                    )
                ],
                reason="ok",
            )

        async def get_orders(self):
            return []

        async def place_order(self, request):
            raise NotImplementedError

    class _PnlEngine:
        def sync_account_mark_to_market(self, **kwargs):
            captured.update(kwargs)

    ownership_store = SimpleNamespace(
        reconcile_broker_positions=lambda **_kwargs: SimpleNamespace(
            changed=False,
            added_unknown=0,
            converted_to_unknown=0,
            removed_stale=0,
            parse_errors=0,
        ),
        get_owner=lambda **_kwargs: "ema20-trend",
    )
    monkeypatch.setattr(
        "app.hub.account_runner.dashboard_bus.get_last_price",
        lambda _symbol: None,
    )
    monkeypatch.setattr(
        "app.hub.account_runner.dashboard_bus.get_last_price_for_instrument",
        lambda *, symbol=None, token=None: 125.0
        if symbol == "BANKNIFTY28JUL2658000CE"
        else None,
    )

    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BrokerClientWithBrokerSymbol(),
        runtime_mode="LIVE",
        state_store=state_store,
        pnl_engine=_PnlEngine(),
        position_ownership_store=ownership_store,
        poll_interval_seconds=1,
    )

    asyncio.run(runner._sync_positions(force=True))

    assert captured["account_unrealized_pnl"] == 50.0
    assert captured["account_gross_exposure"] == 250.0
    assert captured["per_strategy_marks"] == {StrategyId("ema20-trend"): (50.0, 250.0)}
    assert captured["source"] == "broker_sync"
    asyncio.run(runner.stop())


def test_live_sync_positions_tags_snapshot_when_ltp_missing(monkeypatch, caplog):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    state_store = StateStore()
    account = _dummy_account()
    captured = {}

    class _BrokerClientMissingMark:
        async def login(self):
            return None

        async def get_balance(self):
            raise NotImplementedError

        async def get_positions(self):
            return PositionsFetchResult(
                status=PositionsStatus.OK,
                positions=[
                    Position(
                        symbol="NIFTY30JUN2623950CE",
                        quantity=-2,
                        avg_price=100.0,
                        product_type=ProductType.INTRADAY,
                    )
                ],
                reason="ok",
            )

        async def get_orders(self):
            return []

        async def place_order(self, request):
            raise NotImplementedError

    class _PnlEngine:
        def sync_account_mark_to_market(self, **kwargs):
            captured.update(kwargs)

    ownership_store = SimpleNamespace(
        reconcile_broker_positions=lambda **_kwargs: SimpleNamespace(
            changed=False,
            added_unknown=0,
            converted_to_unknown=0,
            removed_stale=0,
            parse_errors=0,
        ),
        get_owner=lambda **_kwargs: "ema20-trend",
    )
    monkeypatch.setattr(
        "app.hub.account_runner.dashboard_bus.get_last_price",
        lambda _symbol: None,
    )
    caplog.set_level("WARNING", logger="app.hub.account_runner")

    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BrokerClientMissingMark(),
        runtime_mode="LIVE",
        state_store=state_store,
        pnl_engine=_PnlEngine(),
        position_ownership_store=ownership_store,
        poll_interval_seconds=1,
    )

    asyncio.run(runner._sync_positions(force=True))

    assert captured["account_unrealized_pnl"] == 0.0
    assert captured["account_gross_exposure"] == 200.0
    assert captured["per_strategy_marks"] == {}
    assert captured["source"] == "broker_sync_stale_mark"
    assert "mark.unavailable" in caplog.text
    asyncio.run(runner.stop())


def test_live_sync_positions_marks_pnl_recovered_when_broker_flat(caplog):
    state_store = StateStore()
    account = _dummy_account()
    captured = {}
    state_store.update_positions_status(
        account.broker_account_id,
        status="BLOCKED",
        last_ok_ts="2026-07-03T06:57:00+00:00",
        last_count=1,
        error_reason="rate_limited",
        blocked_ts="2026-07-03T06:58:00+00:00",
        retry_after_seconds=30.0,
    )

    class _PnlEngine:
        def sync_account_mark_to_market(self, **kwargs):
            captured.update(kwargs)

    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_FlatBrokerClient(),
        runtime_mode="LIVE",
        state_store=state_store,
        pnl_engine=_PnlEngine(),
        poll_interval_seconds=1,
    )
    caplog.set_level("INFO", logger="app.hub.account_runner")

    asyncio.run(runner._sync_positions(force=True))

    assert captured["account_unrealized_pnl"] == 0.0
    assert captured["account_gross_exposure"] == 0.0
    assert captured["per_strategy_marks"] == {}
    assert captured["source"] == "broker_sync_flat"
    status = state_store.get_positions_status(account.broker_account_id)
    assert status["status"] == "OK"
    assert status["last_count"] == 0
    assert status["error_reason"] is None
    assert "event_type=PNL_MARK_RECOVERED_FLAT" in caplog.text
    asyncio.run(runner.stop())
