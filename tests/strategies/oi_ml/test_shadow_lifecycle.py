from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.brokers.base import OrderPurpose, OrderSide, OrderType, ProductType, TimeInForce
from app.risk.option_sell_guard import OptionSellStructure
from app.strategies.oi_ml.order_intents import OiMlOrderIntent, OiMlOrderIntentLeg
from app.strategies.oi_ml.shadow_lifecycle import (
    InMemoryOiMlShadowLifecycleStore,
    OiMlShadowIntentStatus,
    PostgresOiMlShadowLifecycleStore,
    intent_payload,
    record_from_intent,
)


CREATED_AT = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
EXPIRY = date(2026, 5, 21)


def _intent(*, dry_run_only: bool = True) -> OiMlOrderIntent:
    leg = OiMlOrderIntentLeg(
        role="CE_SHORT",
        side=OrderSide.SELL,
        symbol="NIFTY21MAY2625200CE",
        exchange="NFO",
        symbol_token="12345",
        expiry=EXPIRY,
        strike=25200,
        option_type="CE",
        quantity=25,
        price_hint=119.5,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.ENTRY,
        source_snapshot_ts=CREATED_AT,
    )
    return OiMlOrderIntent(
        intent_id="intent-1",
        strategy_id="oi_ml_ce_seller",
        structure=OptionSellStructure.NAKED_SHORT_CE,
        underlying="NIFTY",
        expiry=EXPIRY,
        short_strike=25200,
        quantity=25,
        created_at=CREATED_AT,
        legs=(leg,),
        estimated_net_credit_points=119.5,
        estimated_max_loss_rupees=4000.0,
        dry_run_only=dry_run_only,
        guard_reasons=("allowed",),
    )


def test_intent_payload_is_json_safe_and_marks_dry_run_only():
    payload = intent_payload(_intent())

    assert payload["intent_id"] == "intent-1"
    assert payload["dry_run_only"] is True
    assert payload["structure"] == "NAKED_SHORT_CE"
    assert payload["legs"][0]["side"] == "SELL"
    assert payload["legs"][0]["product_type"] == "INTRADAY"
    assert payload["created_at"] == CREATED_AT.isoformat()


def test_record_from_intent_rejects_non_dry_run_intent():
    with pytest.raises(ValueError, match="dry_run_only"):
        record_from_intent(_intent(dry_run_only=False), record_id=None)


def test_in_memory_store_records_shadow_lifecycle_shape():
    store = InMemoryOiMlShadowLifecycleStore()

    record = store.record_intent(
        _intent(),
        decision_reason="candidate_passed_guard",
        tenant_id="tenant-a",
        broker_account_id="acct-a",
    )

    assert record.record_id == 1
    assert record.status == OiMlShadowIntentStatus.STAGED
    assert record.tenant_id == "tenant-a"
    assert record.broker_account_id == "acct-a"
    assert record.dry_run_only is True
    assert record.intent_payload["dry_run_only"] is True
    assert store.records == [record]


class FakeCursor:
    def __init__(self):
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return {"id": 42, "recorded_at": CREATED_AT}


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj


def test_postgres_store_upserts_shadow_record_without_order_requests():
    conn = FakeConn()
    store = PostgresOiMlShadowLifecycleStore(conn)

    record = store.record_intent(
        _intent(),
        decision_reason="candidate_passed_guard",
        tenant_id="tenant-a",
        broker_account_id="acct-a",
    )

    assert record.record_id == 42
    assert "INSERT INTO \"public\".\"oi_ml_shadow_order_intents\"" in conn.cursor_obj.sql
    assert "ON CONFLICT (intent_id)" in conn.cursor_obj.sql
    assert "TRUE" in conn.cursor_obj.sql
    assert conn.cursor_obj.params["intent_id"] == "intent-1"
    assert conn.cursor_obj.params["status"] == "STAGED"
    assert conn.cursor_obj.params["broker_account_id"] == "acct-a"


def test_postgres_store_rejects_unsafe_table_names():
    with pytest.raises(ValueError, match="Invalid postgres table identifier"):
        PostgresOiMlShadowLifecycleStore(FakeConn(), table_name="public.bad-name")
