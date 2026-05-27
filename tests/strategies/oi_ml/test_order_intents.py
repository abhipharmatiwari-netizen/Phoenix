from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.brokers.base import OrderRequest, OrderSide
from app.data.option_chain_provider import OptionQuote
from app.risk.option_sell_guard import OptionSellGuardResult, OptionSellStructure
from app.strategies.oi_ml.decision import OiMlCandidatePlan
from app.strategies.oi_ml.order_intents import (
    OiMlOrderIntentConfig,
    build_order_intent_from_candidate,
)
from app.strategies.oi_ml.scoring import OiMlScore


CREATED_AT = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)
EXPIRY = date(2026, 5, 21)


def _quote(strike: int, *, option_type: str = "CE", ltp: float = 100.0) -> OptionQuote:
    return OptionQuote(
        snapshot_ts=CREATED_AT - timedelta(seconds=20),
        source_ts=CREATED_AT - timedelta(seconds=30),
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        trading_symbol=f"NIFTY21MAY26{strike}{option_type}",
        exchange="NFO",
        provider="angel",
        symbol_token=f"{strike}{option_type}",
        oi=1000,
        volume=1000,
        iv="12.0",
        delta="0.20",
        gamma="0.0010",
        theta="-3.0",
        vega="6.0",
        bid=max(0.05, ltp - 0.5),
        ask=ltp + 0.5,
        ltp=ltp,
        underlying_ltp=25140.0,
        vix=16.0,
    )


def _plan(
    *,
    structure: OptionSellStructure,
    guard_allowed: bool = True,
    snapshot=None,
    quote=None,
) -> OiMlCandidatePlan:
    short_quote = quote or _quote(25200, ltp=120.0)
    guard = (
        OptionSellGuardResult.allow("allowed")
        if guard_allowed
        else OptionSellGuardResult.reject(["test_reject"])
    )
    return OiMlCandidatePlan(
        quote=short_quote,
        features={"candidate_oi": 1000},
        score=OiMlScore(probability=0.64, predicted_mae_premium=40.0),
        structure=structure,
        premium_received=119.5,
        max_loss_rupees=4000.0,
        guard_result=guard,
        snapshot=tuple(snapshot if snapshot is not None else [short_quote, _quote(25400, ltp=20.0)]),
        metadata={},
    )


def test_builds_inert_bear_call_spread_intent_with_template_roles():
    result = build_order_intent_from_candidate(
        _plan(structure=OptionSellStructure.BEAR_CALL_SPREAD),
        created_at=CREATED_AT,
        config=OiMlOrderIntentConfig(lots=1, lot_size=25, max_spread_loss_rupees=3000.0),
    )

    assert result.ok is True
    intent = result.intent
    assert intent is not None
    assert intent.dry_run_only is True
    assert intent.structure == OptionSellStructure.BEAR_CALL_SPREAD
    assert intent.quantity == 25
    assert intent.estimated_net_credit_points == 99.0
    assert intent.estimated_max_loss_rupees == 2525.0
    assert [leg.role for leg in intent.legs] == ["CE_SHORT", "CE_LONG"]
    assert [leg.side for leg in intent.legs] == [OrderSide.SELL, OrderSide.BUY]
    assert intent.legs[0].symbol == "NIFTY21MAY2625200CE"
    assert intent.legs[1].symbol == "NIFTY21MAY2625400CE"
    assert intent.legs[0].delta == 0.20
    assert intent.metadata["greek_exposure"]["gross_abs_delta"] == 10.0
    assert not isinstance(intent.legs[0], OrderRequest)


def test_spread_intent_rejects_when_long_leg_quote_is_missing():
    result = build_order_intent_from_candidate(
        _plan(
            structure=OptionSellStructure.BEAR_CALL_SPREAD,
            snapshot=[_quote(25200, ltp=120.0)],
        ),
        created_at=CREATED_AT,
        config=OiMlOrderIntentConfig(lots=1, lot_size=25),
    )

    assert result.ok is False
    assert result.reasons == ("missing_long_leg_quote",)
    assert result.metadata["long_strike"] == 25400


def test_spread_intent_rejects_when_actual_credit_is_too_low():
    result = build_order_intent_from_candidate(
        _plan(
            structure=OptionSellStructure.BEAR_CALL_SPREAD,
            snapshot=[_quote(25200, ltp=20.0), _quote(25400, ltp=25.0)],
            quote=_quote(25200, ltp=20.0),
        ),
        created_at=CREATED_AT,
        config=OiMlOrderIntentConfig(lots=1, lot_size=25),
    )

    assert result.ok is False
    assert result.reasons == ("net_credit_below_min",)


def test_spread_intent_rechecks_actual_max_loss_from_both_legs():
    result = build_order_intent_from_candidate(
        _plan(structure=OptionSellStructure.BEAR_CALL_SPREAD),
        created_at=CREATED_AT,
        config=OiMlOrderIntentConfig(lots=1, lot_size=25, max_spread_loss_rupees=2500.0),
    )

    assert result.ok is False
    assert result.reasons == ("estimated_spread_loss_above_limit",)
    assert result.metadata["estimated_max_loss_rupees"] == 2525.0


def test_builds_inert_naked_short_ce_intent_when_candidate_was_guarded():
    result = build_order_intent_from_candidate(
        _plan(structure=OptionSellStructure.NAKED_SHORT_CE),
        created_at=CREATED_AT,
        config=OiMlOrderIntentConfig(lots=2, lot_size=25),
    )

    assert result.ok is True
    intent = result.intent
    assert intent is not None
    assert intent.structure == OptionSellStructure.NAKED_SHORT_CE
    assert intent.quantity == 50
    assert len(intent.legs) == 1
    assert intent.legs[0].side == OrderSide.SELL
    assert intent.estimated_net_credit_points == 119.5
    assert intent.estimated_max_loss_rupees == 4000.0
    assert intent.metadata["risk_note"] == "naked_loss_uses_guard_cap_not_theoretical"


def test_intent_builder_rejects_unguarded_candidates():
    result = build_order_intent_from_candidate(
        _plan(structure=OptionSellStructure.NAKED_SHORT_CE, guard_allowed=False),
        created_at=CREATED_AT,
    )

    assert result.ok is False
    assert result.reasons == ("guard_result_not_allowed",)


def test_intent_builder_scales_lots_from_greek_risk_multiplier():
    plan = _plan(structure=OptionSellStructure.BEAR_CALL_SPREAD)
    plan = OiMlCandidatePlan(
        quote=plan.quote,
        features=plan.features,
        score=plan.score,
        structure=plan.structure,
        premium_received=plan.premium_received,
        max_loss_rupees=plan.max_loss_rupees,
        guard_result=plan.guard_result,
        snapshot=plan.snapshot,
        metadata={"greek_risk": {"lot_multiplier": 0.5}},
    )

    result = build_order_intent_from_candidate(
        plan,
        created_at=CREATED_AT,
        config=OiMlOrderIntentConfig(lots=4, lot_size=25, max_spread_loss_rupees=10000.0),
    )

    assert result.ok is True
    assert result.intent is not None
    assert result.intent.quantity == 50
    assert result.intent.metadata["base_lots"] == 4
    assert result.intent.metadata["effective_lots"] == 2
