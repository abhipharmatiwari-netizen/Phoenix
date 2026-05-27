from types import SimpleNamespace
from datetime import datetime, date, timezone

from app.brokers.base import (
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.risk.option_sell_guard import OptionSellStructure
from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.decision import OiMlEntryAction, OiMlEntryDecision
from app.strategies.oi_ml.order_intents import OiMlOrderIntent, OiMlOrderIntentLeg
from app.strategies.oi_ml_ce_seller import OiMlCeSellerStrategy, OiMlOpenSpread


ENTRY_TS = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)


def _strategy(
    *,
    decision_engine=None,
    order_intent_builder=None,
    shadow_lifecycle_store=None,
    **params,
):
    return OiMlCeSellerStrategy(
        instrument_meta={},
        order_client=SimpleNamespace(),
        risk_manager=SimpleNamespace(),
        env_prefix="NIFTY_OI_ML_",
        underlying_label="NIFTY_IDX",
        params=params,
        decision_engine=decision_engine,
        order_intent_builder=order_intent_builder,
        shadow_lifecycle_store=shadow_lifecycle_store,
    )


def test_scaffold_uses_canonical_strategy_id_and_safe_defaults():
    strategy = _strategy()

    assert strategy._strategy_id == OI_ML_CE_SELLER_ID
    assert strategy.underlying_label == "NIFTY_IDX"
    assert strategy.allow_naked is False
    assert strategy.max_open_spreads == 1
    assert strategy.max_spread_loss_rupees == 5000


def test_scaffold_records_market_data_but_stays_fail_closed():
    strategy = _strategy()
    candle = SimpleNamespace(start_ts=None, c=25000.0)

    strategy.on_tick("NIFTY_IDX", 25000.5)
    strategy.on_bar("NIFTY_IDX", 300, candle, {"ema_20": 24950.0})

    assert strategy.last_price["NIFTY_IDX"] == 25000.0
    assert strategy.no_trade_counts["strategy_scaffold_fail_closed"] == 1
    assert strategy.open_spreads == {}


def test_force_exit_all_clears_scaffold_state_without_order_submission():
    strategy = _strategy()
    strategy.open_spreads["spread-1"] = object()

    strategy.force_exit_all(reason="TEST", submit_orders=True)

    assert strategy.open_spreads == {}


class FakeDecisionEngine:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def evaluate_entry(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


def test_strategy_stages_guarded_candidate_without_order_submission():
    selected = SimpleNamespace(strike=25200)
    decision = OiMlEntryDecision(
        action=OiMlEntryAction.STAGE_ENTRY,
        reason="candidate_passed_guard",
        selected=selected,
    )
    engine = FakeDecisionEngine(decision)
    builder = FakeIntentBuilder(ok=True)
    strategy = _strategy(
        decision_engine=engine,
        order_intent_builder=builder,
        expiry="2026-05-21",
        tenant_id="tenant-a",
        account_id="acct-a",
    )
    candle = SimpleNamespace(start_ts=ENTRY_TS, c=25000.0)

    strategy.on_bar("NIFTY_IDX", 300, candle, {})

    assert strategy.staged_entries == [selected]
    assert strategy.staged_order_intents == [builder.intent]
    assert strategy.no_trade_counts["order_intent_staged_no_order_routing"] == 1
    assert engine.calls[0]["underlying"] == "NIFTY"
    assert engine.calls[0]["tenant_id"] == "tenant-a"
    assert engine.calls[0]["account_id"] == "acct-a"
    assert builder.calls[0]["candidate"] is selected
    assert builder.calls[0]["config"].strategy_id == OI_ML_CE_SELLER_ID


def test_strategy_with_decision_engine_requires_expiry():
    engine = FakeDecisionEngine(
        OiMlEntryDecision(action=OiMlEntryAction.NO_TRADE, reason="unused")
    )
    strategy = _strategy(decision_engine=engine)

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=ENTRY_TS), {})

    assert strategy.no_trade_counts["missing_expiry"] == 1
    assert engine.calls == []


class FakeIntentBuilder:
    def __init__(self, *, ok):
        self.intent = SimpleNamespace(intent_id="intent-1")
        self.ok = ok
        self.calls = []

    def __call__(self, candidate, *, created_at, config):
        self.calls.append(
            {
                "candidate": candidate,
                "created_at": created_at,
                "config": config,
            }
        )
        if self.ok:
            return SimpleNamespace(ok=True, intent=self.intent, reasons=())
        return SimpleNamespace(ok=False, intent=None, reasons=("missing_long_leg_quote",))


def test_strategy_records_rejected_order_intent_without_staging():
    selected = SimpleNamespace(strike=25200)
    decision = OiMlEntryDecision(
        action=OiMlEntryAction.STAGE_ENTRY,
        reason="candidate_passed_guard",
        selected=selected,
    )
    strategy = _strategy(
        decision_engine=FakeDecisionEngine(decision),
        order_intent_builder=FakeIntentBuilder(ok=False),
        expiry="2026-05-21",
    )

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=ENTRY_TS), {})

    assert strategy.staged_entries == []
    assert strategy.staged_order_intents == []
    assert strategy.no_trade_counts["order_intent_rejected:missing_long_leg_quote"] == 1


class FakeShadowStore:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.calls = []
        self.record = SimpleNamespace(record_id=7)

    def record_intent(self, intent, *, decision_reason, tenant_id, broker_account_id):
        self.calls.append(
            {
                "intent": intent,
                "decision_reason": decision_reason,
                "tenant_id": tenant_id,
                "broker_account_id": broker_account_id,
            }
        )
        if self.raises:
            raise RuntimeError("db down")
        return self.record


def test_strategy_records_shadow_lifecycle_before_staging_intent():
    selected = SimpleNamespace(strike=25200)
    decision = OiMlEntryDecision(
        action=OiMlEntryAction.STAGE_ENTRY,
        reason="candidate_passed_guard",
        selected=selected,
    )
    builder = FakeIntentBuilder(ok=True)
    store = FakeShadowStore()
    strategy = _strategy(
        decision_engine=FakeDecisionEngine(decision),
        order_intent_builder=builder,
        shadow_lifecycle_store=store,
        expiry="2026-05-21",
        tenant_id="tenant-a",
        account_id="acct-a",
    )

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=ENTRY_TS), {})

    assert store.calls[0]["intent"] is builder.intent
    assert store.calls[0]["decision_reason"] == "candidate_passed_guard"
    assert store.calls[0]["tenant_id"] == "tenant-a"
    assert store.calls[0]["broker_account_id"] == "acct-a"
    assert strategy.shadow_lifecycle_records == [store.record]
    assert strategy.staged_order_intents == [builder.intent]


def test_strategy_fails_closed_when_shadow_lifecycle_recording_fails():
    selected = SimpleNamespace(strike=25200)
    decision = OiMlEntryDecision(
        action=OiMlEntryAction.STAGE_ENTRY,
        reason="candidate_passed_guard",
        selected=selected,
    )
    builder = FakeIntentBuilder(ok=True)
    strategy = _strategy(
        decision_engine=FakeDecisionEngine(decision),
        order_intent_builder=builder,
        shadow_lifecycle_store=FakeShadowStore(raises=True),
        expiry="2026-05-21",
    )

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=ENTRY_TS), {})

    assert strategy.staged_entries == []
    assert strategy.staged_order_intents == []
    assert strategy.shadow_lifecycle_records == []
    assert strategy.no_trade_counts["shadow_lifecycle_exception:RuntimeError"] == 1


def _intent() -> OiMlOrderIntent:
    created = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
    expiry = date(2026, 5, 21)
    short = OiMlOrderIntentLeg(
        role="CE_SHORT",
        side=OrderSide.SELL,
        symbol="NIFTY21MAY2625200CE",
        exchange="NFO",
        symbol_token="25200CE",
        expiry=expiry,
        strike=25200,
        option_type="CE",
        quantity=65,
        price_hint=100.0,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        source_snapshot_ts=created,
    )
    long = OiMlOrderIntentLeg(
        role="CE_LONG",
        side=OrderSide.BUY,
        symbol="NIFTY21MAY2625400CE",
        exchange="NFO",
        symbol_token="25400CE",
        expiry=expiry,
        strike=25400,
        option_type="CE",
        quantity=65,
        price_hint=20.0,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        source_snapshot_ts=created,
    )
    return OiMlOrderIntent(
        intent_id="intent-1",
        strategy_id=OI_ML_CE_SELLER_ID,
        structure=OptionSellStructure.BEAR_CALL_SPREAD,
        underlying="NIFTY",
        expiry=expiry,
        short_strike=25200,
        quantity=65,
        created_at=created,
        legs=(short, long),
        estimated_net_credit_points=80.0,
        estimated_max_loss_rupees=7800.0,
    )


class FakeIntentBuilderWithIntent:
    def __init__(self, intent):
        self.intent = intent

    def __call__(self, *_args, **_kwargs):
        return SimpleNamespace(ok=True, intent=self.intent, reasons=())


def _routed_strategy(intent, **params):
    selected = SimpleNamespace(quote=None, score=SimpleNamespace(probability=0.7, predicted_mae_premium=20.0))
    decision = OiMlEntryDecision(
        action=OiMlEntryAction.STAGE_ENTRY,
        reason="candidate_passed_guard",
        selected=selected,
    )
    return _strategy(
        decision_engine=FakeDecisionEngine(decision),
        order_intent_builder=FakeIntentBuilderWithIntent(intent),
        expiry="2026-05-21",
        order_routing_enabled=True,
        lot_size=65,
        **params,
    )


def test_strategy_routes_protected_first_spread_entry(monkeypatch):
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        return OrderResponse(
            broker_order_id=f"OID-{len(calls)}",
            status="COMPLETE",
            message="ok",
            filled_quantity=order_req.quantity,
        )

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)
    strategy = _routed_strategy(_intent())
    candle = SimpleNamespace(start_ts=datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc), c=25100.0)

    strategy.on_bar("NIFTY_IDX", 300, candle, {})

    assert [call.symbol for call in calls[:2]] == [
        "NIFTY21MAY2625400CE",
        "NIFTY21MAY2625200CE",
    ]
    assert [call.side for call in calls[:2]] == [OrderSide.BUY, OrderSide.SELL]
    assert calls[0].quantity == 1
    assert calls[1].strategy_context["structure"] == "BEAR_CALL_SPREAD"
    assert len(strategy.open_spreads) == 1


def test_strategy_rolls_back_hedge_when_short_leg_fails(monkeypatch):
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        if order_req.tag == "OI_ML_ENTRY_SHORT":
            return OrderResponse("", "REJECTED", "blocked")
        return OrderResponse(
            broker_order_id=f"OID-{len(calls)}",
            status="COMPLETE",
            message="ok",
            filled_quantity=order_req.quantity,
        )

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)
    strategy = _routed_strategy(_intent())

    strategy.on_bar(
        "NIFTY_IDX",
        300,
        SimpleNamespace(start_ts=datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc), c=25100.0),
        {},
    )

    assert [call.tag for call in calls] == [
        "OI_ML_ENTRY_HEDGE",
        "OI_ML_ENTRY_SHORT",
        "OI_ML_ROLLBACK_HEDGE",
    ]
    assert strategy.open_spreads == {}
    assert strategy.no_trade_counts["spread_entry_rejected:short_leg_rejected_hedge_rollback_submitted"] == 1


def test_strategy_exit_retries_only_residual_lots_after_partial_fill(monkeypatch):
    intent = _intent()
    long_leg = list(intent.legs)[1]
    short_leg = list(intent.legs)[0]
    strategy = _strategy(
        expiry="2026-05-21",
        order_routing_enabled=True,
        lot_size=65,
    )
    strategy.open_spreads["s1"] = OiMlOpenSpread(
        spread_id="s1",
        intent=intent,
        short_leg=short_leg,
        long_leg=long_leg,
        quantity_lots=2,
        remaining_lots=2,
        entry_credit=80.0,
        entry_time=datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc),
    )
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        filled = 1 if order_req.tag.startswith("OI_ML_EXIT") else order_req.quantity
        return OrderResponse("OID", "PARTIALLY_FILLED", "partial", filled_quantity=filled)

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)

    strategy.force_exit_all(reason="TEST")
    strategy.force_exit_all(reason="TEST")

    assert [call.quantity for call in calls if call.symbol == "NIFTY21MAY2625200CE"] == [2, 1]
    assert strategy.open_spreads == {}


def test_strategy_blocks_new_entries_after_cutoff_and_force_exits_at_time_stop(monkeypatch):
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        return OrderResponse("OID", "COMPLETE", "ok", filled_quantity=order_req.quantity)

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)
    intent = _intent()
    strategy = _routed_strategy(intent)
    strategy.open_spreads["s1"] = OiMlOpenSpread(
        spread_id="s1",
        intent=intent,
        short_leg=list(intent.legs)[0],
        long_leg=list(intent.legs)[1],
        quantity_lots=1,
        remaining_lots=1,
        entry_credit=80.0,
        entry_time=datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc),
    )

    strategy.on_bar(
        "NIFTY_IDX",
        300,
        SimpleNamespace(start_ts=datetime(2026, 5, 19, 9, 25, tzinfo=timezone.utc), c=25100.0),
        {},
    )

    assert strategy.no_trade_counts["outside_entry_window"] == 1
    assert [call.tag for call in calls] == ["OI_ML_EXIT_TIME_STOP", "OI_ML_EXIT_HEDGE_TIME_STOP"]
    assert strategy.open_spreads == {}


def test_strategy_flattens_any_residual_spread_after_eod_cap(monkeypatch):
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        return OrderResponse("OID", "COMPLETE", "ok", filled_quantity=order_req.quantity)

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)
    intent = _intent()
    strategy = _strategy(
        expiry="2026-05-21",
        order_routing_enabled=True,
        lot_size=65,
    )
    strategy.open_spreads["s1"] = OiMlOpenSpread(
        spread_id="s1",
        intent=intent,
        short_leg=list(intent.legs)[0],
        long_leg=list(intent.legs)[1],
        quantity_lots=1,
        remaining_lots=1,
        entry_credit=80.0,
        entry_time=datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc),
    )

    strategy.on_bar(
        "NIFTY_IDX",
        300,
        SimpleNamespace(start_ts=datetime(2026, 5, 19, 9, 51, tzinfo=timezone.utc), c=25100.0),
        {},
    )

    assert [call.tag for call in calls] == ["OI_ML_EXIT_EOD", "OI_ML_EXIT_HEDGE_EOD"]
    assert all(call.purpose.name == "EXIT" for call in calls)
    assert strategy.open_spreads == {}


def test_strategy_exits_open_spread_when_delta_rises_above_greek_stop(monkeypatch):
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        return OrderResponse("OID", "COMPLETE", "ok", filled_quantity=order_req.quantity)

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)
    intent = _intent()
    strategy = _strategy(
        expiry="2026-05-21",
        order_routing_enabled=True,
        lot_size=65,
    )
    strategy.open_spreads["s1"] = OiMlOpenSpread(
        spread_id="s1",
        intent=intent,
        short_leg=list(intent.legs)[0],
        long_leg=list(intent.legs)[1],
        quantity_lots=1,
        remaining_lots=1,
        entry_credit=80.0,
        entry_time=ENTRY_TS,
    )
    strategy.mark_spread_greeks("s1", delta=0.48, gamma=0.0010, iv=12.0)

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=ENTRY_TS, c=25100.0), {})

    assert [call.tag for call in calls] == [
        "OI_ML_EXIT_GREEK_DELTA_STOP",
        "OI_ML_EXIT_HEDGE_GREEK_DELTA_STOP",
    ]
    assert strategy.open_spreads == {}


def test_strategy_tightens_loss_stop_when_iv_expands(monkeypatch):
    calls = []

    def fake_bridge(*, strategy_id, order_req, tenant_id=None, broker_account_id=None):
        calls.append(order_req)
        return OrderResponse("OID", "COMPLETE", "ok", filled_quantity=order_req.quantity)

    monkeypatch.setattr("app.strategies.oi_ml_ce_seller.place_order_via_bridge", fake_bridge)
    intent = _intent()
    strategy = _strategy(
        expiry="2026-05-21",
        order_routing_enabled=True,
        lot_size=65,
        stop_loss_mult_credit=1.8,
    )
    strategy.open_spreads["s1"] = OiMlOpenSpread(
        spread_id="s1",
        intent=intent,
        short_leg=list(intent.legs)[0],
        long_leg=list(intent.legs)[1],
        quantity_lots=1,
        remaining_lots=1,
        entry_credit=80.0,
        entry_time=ENTRY_TS,
        metadata={
            "entry_greeks": {"iv": 10.0, "abs_delta": 0.20, "abs_gamma": 0.0010},
            "current_greeks": {"iv": 12.0, "abs_delta": 0.20, "abs_gamma": 0.0010},
        },
    )
    strategy.last_price["NIFTY21MAY2625200CE"] = 120.0
    strategy.last_price["NIFTY21MAY2625400CE"] = 10.0

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=ENTRY_TS, c=25100.0), {})

    assert [call.tag for call in calls] == [
        "OI_ML_EXIT_GREEK_TIGHT_LOSS_STOP",
        "OI_ML_EXIT_HEDGE_GREEK_TIGHT_LOSS_STOP",
    ]
    assert strategy.open_spreads == {}
