from types import SimpleNamespace

from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.decision import OiMlEntryAction, OiMlEntryDecision
from app.strategies.oi_ml_ce_seller import OiMlCeSellerStrategy


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

    assert strategy.last_price["NIFTY_IDX"] == 25000.5
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
    candle = SimpleNamespace(start_ts=None, c=25000.0)

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

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=None), {})

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

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=None), {})

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

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=None), {})

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

    strategy.on_bar("NIFTY_IDX", 300, SimpleNamespace(start_ts=None), {})

    assert strategy.staged_entries == []
    assert strategy.staged_order_intents == []
    assert strategy.shadow_lifecycle_records == []
    assert strategy.no_trade_counts["shadow_lifecycle_exception:RuntimeError"] == 1
