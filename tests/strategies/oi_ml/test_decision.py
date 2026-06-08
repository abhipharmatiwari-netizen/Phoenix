from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from app.data.option_chain_provider import OptionQuote
from app.risk.kill_switch import KillSwitchManager, KillSwitchScope
from app.risk.option_sell_guard import OptionSellGuardConfig, OptionSellStructure
from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.decision import (
    OiMlCeDecisionEngine,
    OiMlDecisionConfig,
    OiMlEntryAction,
)
from app.strategies.oi_ml.greek_risk import OiMlGreekRiskConfig
from app.strategies.oi_ml.scoring import ConstantOiMlScorer


DECISION_TS = datetime(2026, 5, 19, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
EXPIRY = date(2026, 5, 21)


def _quote(
    strike: int,
    option_type: str,
    *,
    ltp: float = 120.0,
    oi: int = 1000,
    vix: float = 16.0,
    spot: float = 25140.0,
    delta: float = 0.20,
    gamma: float = 0.0010,
    theta: float = -3.0,
    vega: float = 6.0,
    minute_offset: int = 0,
    source_lag_seconds: int = 15,
) -> OptionQuote:
    ts = DECISION_TS + timedelta(minutes=minute_offset)
    return OptionQuote(
        snapshot_ts=ts,
        source_ts=ts - timedelta(seconds=source_lag_seconds),
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        trading_symbol=f"NIFTY21MAY26{strike}{option_type}",
        exchange="NFO",
        provider="angel",
        symbol_token=f"{strike}{option_type}",
        oi=oi,
        volume=1000,
        iv="12.0",
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        bid=max(0.05, ltp - 0.5),
        ask=ltp + 0.5,
        ltp=ltp,
        underlying_ltp=spot,
        vix=vix,
    )


class FakeRepository:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def fetch_latest_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.snapshot


def _config(**overrides) -> OiMlDecisionConfig:
    base = {
        "provider": "angel",
        "lot_size": 25,
        "spread_width_points": 150.0,
        "max_candidates_per_decision": 3,
        "guard_config": OptionSellGuardConfig(max_spread_loss_rupees=5000.0),
    }
    base.update(overrides)
    return OiMlDecisionConfig(**base)


def test_decision_engine_stages_first_guarded_spread_candidate():
    snapshot = [
        _quote(25100, "CE", ltp=130.0, oi=500, delta=0.38),
        _quote(25200, "CE", ltp=120.0, oi=2500, delta=0.20),
        _quote(25300, "CE", ltp=115.0, oi=100, delta=0.12),
        _quote(25200, "PE", ltp=80.0, oi=700),
    ]
    repo = FakeRepository(snapshot)
    engine = OiMlCeDecisionEngine(
        repo,
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=40.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.STAGE_ENTRY
    assert decision.reason == "candidate_passed_guard"
    assert decision.selected is not None
    assert decision.selected.quote.strike == 25200
    assert decision.selected.structure == OptionSellStructure.BEAR_CALL_SPREAD
    assert decision.selected.guard_result.allowed is True
    assert decision.selected.metadata["greek_risk"]["oi_wall_present"] is True
    assert decision.selected.metadata["greek_risk"]["abs_delta"] == 0.2
    assert repo.calls[0]["provider"] == "angel"
    assert repo.calls[0]["min_snapshot_ts"] == DECISION_TS - timedelta(seconds=120)


def test_decision_engine_blocks_candidate_generation_when_iv_is_missing():
    snapshot = [
        _quote(25100, "CE", ltp=130.0, oi=500, vix=16.0, spot=25140.0, delta=0.38),
        _quote(25200, "CE", ltp=120.0, oi=2500, vix=16.0, spot=25140.0),
        _quote(25300, "CE", ltp=110.0, oi=100, vix=16.0, spot=25140.0, delta=0.12),
        _quote(25200, "PE", ltp=80.0, oi=700, vix=16.0, spot=25140.0),
    ]
    snapshot = [replace(row, iv=None) for row in snapshot]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=40.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert decision.reason == "candidate_generation_blocked:missing_iv"


def test_decision_engine_blocks_candidate_generation_when_greeks_are_missing():
    snapshot = [
        _quote(25200, "CE", ltp=120.0, oi=2500, delta=None),
        _quote(25350, "CE", ltp=110.0, oi=1000, gamma=None),
    ]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=40.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert decision.reason == "candidate_generation_blocked:missing_greeks"


def test_decision_engine_blocks_candidate_generation_when_source_is_stale():
    snapshot = [
        _quote(25200, "CE", ltp=120.0, oi=2500, source_lag_seconds=240),
        _quote(25350, "CE", ltp=110.0, oi=1000, source_lag_seconds=240),
    ]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=40.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert decision.reason == "candidate_generation_blocked:stale_source_seconds"


def test_decision_engine_fails_closed_without_snapshot():
    engine = OiMlCeDecisionEngine(
        FakeRepository([]),
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=40.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert decision.reason == "no_fresh_option_snapshot"


def test_decision_engine_rejects_when_guard_vetoes_all_candidates():
    snapshot = [
        _quote(25100, "CE", ltp=130.0, oi=500, delta=0.38),
        _quote(25200, "CE", ltp=120.0, oi=2500),
        _quote(25300, "CE", ltp=110.0, oi=100, delta=0.12),
    ]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=200.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert decision.reason == "all_candidates_rejected_by_guard"
    assert "predicted_mae_above_limit" in decision.evaluated[0].guard_result.reasons


def test_decision_engine_can_stage_naked_candidate_only_when_enabled_and_tight():
    snapshot = [
        _quote(25100, "CE", ltp=130.0, oi=500, vix=17.0, delta=0.38),
        _quote(25200, "CE", ltp=120.0, oi=2500, vix=17.0),
        _quote(25300, "CE", ltp=110.0, oi=100, vix=17.0, delta=0.12),
    ]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.72, predicted_mae_premium=40.0),
        config=_config(allow_naked=True),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.STAGE_ENTRY
    assert decision.selected is not None
    assert decision.selected.structure == OptionSellStructure.NAKED_SHORT_CE


def test_decision_engine_rejects_candidate_with_excessive_delta():
    snapshot = [
        _quote(25100, "CE", ltp=130.0, oi=500, delta=0.42),
        _quote(25200, "CE", ltp=120.0, oi=2500, delta=0.41),
        _quote(25300, "CE", ltp=110.0, oi=100, delta=0.39),
    ]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.72, predicted_mae_premium=40.0),
        config=_config(),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert "delta_above_max" in decision.evaluated[0].guard_result.reasons


def test_decision_engine_forces_spread_and_scales_size_on_high_vega():
    snapshot = [
        _quote(25100, "CE", ltp=130.0, oi=500, delta=0.38),
        _quote(25200, "CE", ltp=120.0, oi=2500, vega=8.5),
        _quote(25300, "CE", ltp=110.0, oi=100, delta=0.12),
    ]
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.72, predicted_mae_premium=40.0),
        config=_config(allow_naked=True),
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.STAGE_ENTRY
    assert decision.selected is not None
    assert decision.selected.structure == OptionSellStructure.BEAR_CALL_SPREAD
    assert decision.selected.metadata["greek_risk"]["force_spread"] is True
    assert decision.selected.metadata["greek_risk"]["lot_multiplier"] == 0.5


def test_decision_engine_respects_strategy_kill_switch():
    snapshot = [_quote(25200, "CE", ltp=120.0, oi=1000)]
    manager = KillSwitchManager(audit_fn=lambda **_: None)
    manager.trip(
        KillSwitchScope.STRATEGY,
        OI_ML_CE_SELLER_ID,
        reason="daily_loss",
        actor="test",
    )
    engine = OiMlCeDecisionEngine(
        FakeRepository(snapshot),
        ConstantOiMlScorer(probability=0.64, predicted_mae_premium=40.0),
        config=_config(),
        kill_switch_manager=manager,
    )

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert "kill_switch_tripped" in decision.evaluated[0].guard_result.reasons


def test_decision_engine_fails_closed_when_scorer_is_missing():
    snapshot = [_quote(25200, "CE", ltp=120.0, oi=1000)]
    engine = OiMlCeDecisionEngine(FakeRepository(snapshot), config=_config())

    decision = engine.evaluate_entry(
        underlying="NIFTY",
        expiry=EXPIRY,
        decision_ts=DECISION_TS,
    )

    assert decision.action == OiMlEntryAction.NO_TRADE
    assert decision.reason == "model_scorer_missing"
