from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.brokers.base import OrderPurpose, OrderRequest, OrderResponse, OrderSide, OrderType, ProductType, TimeInForce
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.orders.order_outbox import OrderOutboxRecord, _serialize_order_request, _serialize_order_response
from app.orders import router as router_module
from app.orders.router import OrderRouter


class _FakeStateStore:
    def get_order_snapshot(self, broker_account_id):
        _ = broker_account_id
        return []

    def get_orders(self, broker_account_id):
        _ = broker_account_id
        return []

    def set_last_order_response(self, broker_account_id, response):
        _ = (broker_account_id, response)


class _FakeRunner:
    def __init__(self, response: OrderResponse | None = None):
        self.is_running = True
        self.response = response or OrderResponse(
            broker_order_id="OID-PENDING",
            status="OPEN",
            message="accepted",
        )
        self.calls = 0

    async def place_order(self, order_req: OrderRequest) -> OrderResponse:
        _ = order_req
        self.calls += 1
        return self.response


class _FakeHub:
    def __init__(self, runners: dict[str, _FakeRunner]):
        self._runners = runners

    def get_runner(self, broker_account_id):
        return self._runners.get(str(broker_account_id))


class _FakeOutbox:
    def __init__(self, records: list[OrderOutboxRecord]):
        self._records = {
            (r.tenant_id, r.broker_account_id, r.strategy_id, r.submission_key): r
            for r in records
        }

    def _key(self, *, tenant_id, broker_account_id, strategy_id, submission_key):
        return str(tenant_id), str(broker_account_id), str(strategy_id), str(submission_key)

    def list_active_submissions(self, *, limit: int = 500):
        _ = limit
        return list(self._records.values())

    def mark_submitting(self, *, tenant_id, broker_account_id, strategy_id, submission_key, updated_at):
        key = self._key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        record = self._records[key]
        updated = replace(record, status="SUBMITTING", updated_at=updated_at)
        self._records[key] = updated
        return updated

    def record_response(self, *, tenant_id, broker_account_id, strategy_id, submission_key, response, updated_at, recovery_action=None):
        key = self._key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        record = self._records[key]
        updated = replace(
            record,
            status="ACKED",
            broker_order_id=str(response.broker_order_id or ""),
            broker_response=_serialize_order_response(response),
            updated_at=updated_at,
            recovery_action=recovery_action,
        )
        self._records[key] = updated
        return updated

    def record_submit_error(self, *, tenant_id, broker_account_id, strategy_id, submission_key, error_text, updated_at):
        key = self._key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        record = self._records[key]
        updated = replace(record, last_error=str(error_text), updated_at=updated_at)
        self._records[key] = updated
        return updated


def _order_request(*, purpose: OrderPurpose | None = None) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY24APR22000CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=purpose,
    )


def _record(*, tenant_id: str, broker_account_id: str, strategy_id: str, submission_key: str, status: str) -> OrderOutboxRecord:
    now = datetime.now(timezone.utc)
    return OrderOutboxRecord(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        submission_key=submission_key,
        hub_order_id=f"hub-{submission_key}",
        status=status,
        order_request=_serialize_order_request(_order_request(purpose=OrderPurpose.ENTRY)),
        created_at=now,
        updated_at=now,
        broker_order_id=f"broker-{submission_key}" if status == "ACKED" else None,
        broker_response=_serialize_order_response(
            OrderResponse(
                broker_order_id=f"broker-{submission_key}",
                status="OPEN",
                message="accepted",
            )
        )
        if status == "ACKED"
        else None,
    )


@pytest.mark.asyncio
async def test_recover_submission_outbox_logs_rich_summary():
    outbox = _FakeOutbox(
        [
            _record(
                tenant_id="tenant-1",
                broker_account_id="A1",
                strategy_id="strategy-a",
                submission_key="pending-1",
                status="PENDING",
            ),
            _record(
                tenant_id="tenant-1",
                broker_account_id="A2",
                strategy_id="strategy-b",
                submission_key="acked-1",
                status="ACKED",
            ),
        ]
    )
    hub = _FakeHub(
        {
            "A1": _FakeRunner(
                OrderResponse(
                    broker_order_id="OID-RECOVERED",
                    status="OPEN",
                    message="accepted",
                )
            ),
            "A2": _FakeRunner(),
        }
    )
    router = OrderRouter(
        hub=hub,
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=_FakeStateStore(),
        submission_outbox=outbox,
    )

    summary = await router.recover_submission_outbox()

    assert summary["scanned"] == 2
    assert summary["rehydrated"] == 1
    assert summary["replayed"] == 1
    assert summary["adopted"] == 0
    assert summary["failed"] == 0
    assert summary["unresolved_active"] == 0
    assert summary["tenant_count"] == 1
    assert summary["broker_account_count"] == 2
    assert summary["tenant_record_counts"] == {"tenant-1": 2}
    assert summary["broker_account_record_counts"] == {"A1": 1, "A2": 1}
    assert router.last_recovery_summary()["replayed"] == 1


@pytest.mark.asyncio
async def test_submit_order_blocks_new_entries_when_startup_recovery_is_gated(monkeypatch):
    settings = type(
        "_Settings",
        (),
        {"order_router_enforce_idempotency": False},
    )()
    monkeypatch.setattr(router_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        router_module,
        "get_runtime_settings",
        lambda **kwargs: settings,
    )
    router = OrderRouter(
        hub=_FakeHub({}),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=_FakeStateStore(),
        submission_outbox=_FakeOutbox([]),
    )
    router.set_startup_recovery_gate(
        block_new_entries=True,
        reason="startup_recovery_degraded_failed_1_unresolved_0",
        summary={"failed": 1, "unresolved_active": 0},
    )

    hub_order_id, response = await router.submit_order(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("strategy-a"),
        order_req=_order_request(purpose=OrderPurpose.ENTRY),
    )

    assert hub_order_id
    assert response.status == "REJECTED"
    assert response.broker_order_id == "STARTUP_RECOVERY_DEGRADED"
    assert response.message == "startup_recovery_degraded_failed_1_unresolved_0"
