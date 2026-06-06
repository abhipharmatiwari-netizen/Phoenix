"""Guarded entry-decision engine for the intraday OI/ML CE seller."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from app.data.option_chain_provider import OptionQuote
from app.features.oi_features import build_oi_features
from app.risk.option_sell_guard import (
    OptionSellGuardConfig,
    OptionSellGuardContext,
    OptionSellGuardResult,
    OptionSellStructure,
    evaluate_option_sell_guard,
)
from app.strategies.identifiers import OI_ML_CE_SELLER_ID
from app.strategies.oi_ml.dataset import (
    OiMlDatasetConfig,
    candidate_quality_failure_reason,
    select_candidate_quotes,
)
from app.strategies.oi_ml.greek_risk import (
    OiMlGreekRiskAssessment,
    OiMlGreekRiskConfig,
    assess_candidate_greek_risk,
)
from app.strategies.oi_ml.scoring import MissingOiMlScorer, OiMlScore, OiMlScorer


class OiMlEntryAction(str, Enum):
    """Entry engine outcome."""

    NO_TRADE = "NO_TRADE"
    STAGE_ENTRY = "STAGE_ENTRY"


@dataclass(frozen=True)
class OiMlDecisionConfig:
    """Runtime decision policy before any order construction."""

    strategy_id: str = OI_ML_CE_SELLER_ID
    option_type: str = "CE"
    provider: str | None = None
    max_snapshot_age_seconds: int = 120
    min_premium: float = 1.0
    min_oi: int = 1
    min_otm_points: float = 0.0
    max_otm_points: float | None = None
    max_candidates_per_decision: int = 6
    require_source_ts: bool = True
    require_iv: bool = True
    require_greeks: bool = True
    wall_multiple: float = 2.0
    lot_size: int = 65
    spread_width_points: float = 200.0
    allow_naked: bool = False
    guard_config: OptionSellGuardConfig = field(default_factory=OptionSellGuardConfig)
    greek_risk_config: OiMlGreekRiskConfig = field(default_factory=OiMlGreekRiskConfig)


@dataclass(frozen=True)
class OiMlCandidatePlan:
    """A scored option candidate and its guard decision."""

    quote: OptionQuote
    features: Mapping[str, Any]
    score: OiMlScore
    structure: OptionSellStructure
    premium_received: float
    max_loss_rupees: float
    guard_result: OptionSellGuardResult
    snapshot: tuple[OptionQuote, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OiMlEntryDecision:
    """Result returned to the strategy.

    ``STAGE_ENTRY`` means a candidate passed all current decision gates. It is
    intentionally not an order instruction; the strategy must still construct
    legs and route them through Phoenix's normal order path in a later slice.
    """

    action: OiMlEntryAction
    reason: str
    selected: OiMlCandidatePlan | None = None
    evaluated: tuple[OiMlCandidatePlan, ...] = ()


class OiMlCeDecisionEngine:
    """Fetch latest OI snapshot, score candidates, and apply the sell guard."""

    def __init__(
        self,
        repository: Any,
        scorer: OiMlScorer | None = None,
        *,
        config: OiMlDecisionConfig | None = None,
        kill_switch_manager: Any = None,
    ) -> None:
        self.repository = repository
        self.scorer = scorer or MissingOiMlScorer()
        self.config = config or OiMlDecisionConfig()
        self.kill_switch_manager = kill_switch_manager

    def evaluate_entry(
        self,
        *,
        underlying: str,
        expiry: date,
        decision_ts: datetime,
        tenant_id: str | None = None,
        account_id: str | None = None,
    ) -> OiMlEntryDecision:
        decision = _aware_utc(decision_ts)
        snapshot = self._fetch_snapshot(
            underlying=underlying,
            expiry=expiry,
            decision_ts=decision,
        )
        if not snapshot:
            return OiMlEntryDecision(
                action=OiMlEntryAction.NO_TRADE,
                reason="no_fresh_option_snapshot",
            )

        candidates = select_candidate_quotes(
            snapshot,
            decision_ts=decision,
            config=self._dataset_config(),
        )
        if not candidates:
            quality_reason = candidate_quality_failure_reason(
                snapshot,
                decision_ts=decision,
                config=self._dataset_config(),
            )
            return OiMlEntryDecision(
                action=OiMlEntryAction.NO_TRADE,
                reason=(
                    f"candidate_generation_blocked:{quality_reason}"
                    if quality_reason
                    else "no_candidate_quotes"
                ),
            )

        evaluated: list[OiMlCandidatePlan] = []
        for candidate in candidates:
            try:
                plan = self._evaluate_candidate(
                    snapshot=snapshot,
                    candidate=candidate,
                    decision_ts=decision,
                    tenant_id=tenant_id,
                    account_id=account_id,
                )
            except Exception as exc:
                return OiMlEntryDecision(
                    action=OiMlEntryAction.NO_TRADE,
                    reason=f"decision_engine_error:{type(exc).__name__}",
                    evaluated=tuple(evaluated),
                )
            evaluated.append(plan)

        allowed = [plan for plan in evaluated if plan.guard_result.allowed]
        if not allowed:
            return OiMlEntryDecision(
                action=OiMlEntryAction.NO_TRADE,
                reason="all_candidates_rejected_by_guard",
                evaluated=tuple(evaluated),
            )

        selected = max(allowed, key=_selection_key)
        return OiMlEntryDecision(
            action=OiMlEntryAction.STAGE_ENTRY,
            reason="candidate_passed_guard",
            selected=selected,
            evaluated=tuple(evaluated),
        )

    def _fetch_snapshot(
        self,
        *,
        underlying: str,
        expiry: date,
        decision_ts: datetime,
    ) -> list[OptionQuote]:
        min_snapshot_ts = decision_ts - timedelta(
            seconds=max(0, int(self.config.max_snapshot_age_seconds))
        )
        return list(
            self.repository.fetch_latest_snapshot(
                underlying=underlying,
                expiry=expiry,
                decision_ts=decision_ts,
                min_snapshot_ts=min_snapshot_ts,
                provider=self.config.provider,
            )
        )

    def _evaluate_candidate(
        self,
        *,
        snapshot: Sequence[OptionQuote],
        candidate: OptionQuote,
        decision_ts: datetime,
        tenant_id: str | None,
        account_id: str | None,
    ) -> OiMlCandidatePlan:
        features = build_oi_features(
            snapshot,
            candidate_strike=candidate.strike,
            option_type=candidate.option_type,
            decision_ts=decision_ts,
            underlying_ltp=_spot_from_snapshot(snapshot),
            wall_multiple=self.config.wall_multiple,
        )
        greek_risk = assess_candidate_greek_risk(
            candidate,
            features=features,
            config=self.config.greek_risk_config,
        )
        score = self.scorer.score(features)
        structure = self._preferred_structure(
            score=score,
            quote=candidate,
            greek_risk=greek_risk,
        )
        premium = _premium_received(candidate)
        max_loss = self._max_loss_rupees(
            structure=structure,
            premium_received=premium,
        )
        guard_cfg = replace(
            self.config.guard_config,
            strategy_id=self.config.strategy_id,
            allow_naked=bool(self.config.allow_naked),
        )
        guard_result = evaluate_option_sell_guard(
            OptionSellGuardContext(
                now=decision_ts,
                structure=structure,
                quote=candidate,
                ml_score=score.probability,
                predicted_mae_premium=score.predicted_mae_premium,
                premium_received=premium,
                max_loss_rupees=max_loss,
                vix=_vix_from_quote_or_snapshot(candidate, snapshot),
                strategy_id=self.config.strategy_id,
                option_type=self.config.option_type,
                tenant_id=tenant_id,
                account_id=account_id,
                kill_switch_manager=self.kill_switch_manager,
            ),
            guard_cfg,
        )
        guard_result = _apply_greek_risk_guard(guard_result, greek_risk)
        return OiMlCandidatePlan(
            quote=candidate.normalized(),
            features=features,
            score=score,
            structure=structure,
            premium_received=premium,
            max_loss_rupees=max_loss,
            guard_result=guard_result,
            snapshot=tuple(quote.normalized() for quote in snapshot),
            metadata={"greek_risk": dict(greek_risk.metadata)},
        )

    def _dataset_config(self) -> OiMlDatasetConfig:
        return OiMlDatasetConfig(
            option_type=self.config.option_type,
            max_snapshot_age_seconds=self.config.max_snapshot_age_seconds,
            min_premium=self.config.min_premium,
            min_oi=self.config.min_oi,
            min_otm_points=self.config.min_otm_points,
            max_otm_points=self.config.max_otm_points,
            max_candidates_per_decision=self.config.max_candidates_per_decision,
            require_source_ts=self.config.require_source_ts,
            require_iv=self.config.require_iv,
            require_greeks=self.config.require_greeks,
            wall_multiple=self.config.wall_multiple,
        )

    def _preferred_structure(
        self,
        *,
        score: OiMlScore,
        quote: OptionQuote,
        greek_risk: OiMlGreekRiskAssessment,
    ) -> OptionSellStructure:
        if greek_risk.force_spread:
            return OptionSellStructure.BEAR_CALL_SPREAD
        vix = _float(quote.vix)
        guard = self.config.guard_config
        if (
            self.config.allow_naked
            and float(score.probability) >= float(guard.min_naked_ml_score)
            and vix is not None
            and vix < float(guard.naked_max_vix)
        ):
            return OptionSellStructure.NAKED_SHORT_CE
        return OptionSellStructure.BEAR_CALL_SPREAD

    def _max_loss_rupees(
        self,
        *,
        structure: OptionSellStructure,
        premium_received: float,
    ) -> float:
        lot_size = max(1, int(self.config.lot_size))
        if structure is OptionSellStructure.NAKED_SHORT_CE:
            return float(self.config.guard_config.max_naked_loss_rupees)
        width = max(0.0, float(self.config.spread_width_points))
        return max(0.0, (width - float(premium_received)) * lot_size)


def _premium_received(quote: OptionQuote) -> float:
    bid = _float(quote.bid)
    if bid is not None and bid > 0:
        return bid
    mid = _mid(quote)
    if mid is not None and mid > 0:
        return mid
    ltp = _float(quote.ltp)
    if ltp is not None and ltp > 0:
        return ltp
    return 0.0


def _apply_greek_risk_guard(
    guard_result: OptionSellGuardResult,
    greek_risk: OiMlGreekRiskAssessment,
) -> OptionSellGuardResult:
    metadata = {
        **dict(guard_result.metadata or {}),
        "greek_risk": dict(greek_risk.metadata),
    }
    if greek_risk.allowed:
        return OptionSellGuardResult(
            allowed=guard_result.allowed,
            decision=guard_result.decision,
            reasons=guard_result.reasons,
            required_structure=guard_result.required_structure,
            metadata=metadata,
        )
    return OptionSellGuardResult.reject(
        list(guard_result.reasons) + list(greek_risk.reasons),
        metadata=metadata,
    )


def _selection_key(plan: OiMlCandidatePlan) -> tuple[float, float, float, float, float, int]:
    greek = dict(plan.metadata.get("greek_risk") or {})
    delta_distance = _float(greek.get("delta_distance_from_target"))
    wall_multiple = _float(plan.features.get("oi_wall_multiple"))
    stress = _float(greek.get("stress_score"))
    return (
        float(plan.score.probability),
        -(delta_distance if delta_distance is not None else 1.0),
        wall_multiple if wall_multiple is not None else 0.0,
        -(stress if stress is not None else 0.0),
        -plan.max_loss_rupees,
        int(plan.quote.oi or 0),
    )


def _mid(quote: OptionQuote) -> float | None:
    bid = _float(quote.bid)
    ask = _float(quote.ask)
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _spot_from_snapshot(snapshot: Sequence[OptionQuote]) -> float | None:
    for quote in snapshot:
        value = _float(quote.underlying_ltp)
        if value is not None and value > 0:
            return value
    return None


def _vix_from_quote_or_snapshot(
    candidate: OptionQuote,
    snapshot: Sequence[OptionQuote],
) -> float | None:
    value = _float(candidate.vix)
    if value is not None:
        return value
    for quote in snapshot:
        value = _float(quote.vix)
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "OiMlCandidatePlan",
    "OiMlCeDecisionEngine",
    "OiMlDecisionConfig",
    "OiMlEntryAction",
    "OiMlEntryDecision",
]
