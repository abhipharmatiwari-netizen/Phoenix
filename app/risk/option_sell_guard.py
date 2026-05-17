"""Pre-trade guard for intraday option-selling strategies.

The guard is deliberately pure decision logic. It does not submit, modify, or
cancel orders; callers must pass explicit trade context and handle the returned
decision before routing anything to the broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from app.data.option_chain_provider import (
    OptionQuote,
    quality_flags_for_quote,
)
from app.risk.kill_switch import KillSwitchManager
from app.strategies.identifiers import OI_ML_CE_SELLER_ID


IST = timezone(timedelta(hours=5, minutes=30))


class OptionSellStructure(str, Enum):
    """Supported entry structures for the OI/ML CE seller."""

    NAKED_SHORT_CE = "NAKED_SHORT_CE"
    BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"


class OptionSellGuardDecision(str, Enum):
    """High-level guard outcome."""

    ALLOW = "ALLOW"
    REJECT = "REJECT"
    REQUIRE_SPREAD = "REQUIRE_SPREAD"


@dataclass(frozen=True)
class OptionSellGuardConfig:
    """Risk policy for the intraday OI/ML CE-seller guard."""

    strategy_id: str = OI_ML_CE_SELLER_ID
    entry_start_time: time = time(9, 50)
    entry_end_time: time = time(14, 15)
    hard_squareoff_time: time = time(15, 20)
    max_quote_age_seconds: int = 120
    max_entry_vix: float = 22.0
    naked_max_vix: float = 18.0
    min_spread_ml_score: float = 0.55
    min_naked_ml_score: float = 0.70
    max_mae_to_premium: float = 1.20
    max_spread_loss_rupees: float = 5000.0
    max_naked_loss_rupees: float = 6000.0
    allow_naked: bool = False
    require_kill_switch_manager: bool = False


@dataclass(frozen=True)
class OptionSellGuardContext:
    """Trade context required before a short-option entry is allowed."""

    now: datetime
    structure: OptionSellStructure | str
    quote: OptionQuote | None
    ml_score: float | int | str | None
    predicted_mae_premium: float | int | str | None
    premium_received: float | int | str | None
    max_loss_rupees: float | int | str | None
    vix: float | int | str | None
    strategy_id: str = OI_ML_CE_SELLER_ID
    option_type: str = "CE"
    is_exit: bool = False
    tenant_id: str | None = None
    account_id: str | None = None
    kill_switch_manager: KillSwitchManager | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionSellGuardResult:
    """Detailed guard result for logs, tests, and strategy branching."""

    allowed: bool
    decision: OptionSellGuardDecision
    reasons: tuple[str, ...] = ()
    required_structure: OptionSellStructure | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, *reasons: str) -> "OptionSellGuardResult":
        return cls(
            allowed=True,
            decision=OptionSellGuardDecision.ALLOW,
            reasons=tuple(reasons or ("allowed",)),
        )

    @classmethod
    def reject(
        cls,
        reasons: list[str],
        *,
        required_structure: OptionSellStructure | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "OptionSellGuardResult":
        decision = (
            OptionSellGuardDecision.REQUIRE_SPREAD
            if required_structure is not None
            else OptionSellGuardDecision.REJECT
        )
        return cls(
            allowed=False,
            decision=decision,
            reasons=tuple(reasons),
            required_structure=required_structure,
            metadata=dict(metadata or {}),
        )


def evaluate_option_sell_guard(
    context: OptionSellGuardContext,
    config: OptionSellGuardConfig | None = None,
) -> OptionSellGuardResult:
    """Evaluate whether a new option-selling entry may be routed.

    Exit orders bypass the entry guard because kill-switch and EOD workflows
    must be able to flatten risk even after entries are blocked.
    """

    cfg = config or OptionSellGuardConfig()
    if str(context.strategy_id) != str(cfg.strategy_id):
        return OptionSellGuardResult.allow("not_guarded_strategy")
    if context.is_exit:
        return OptionSellGuardResult.allow("exit_order_bypass")

    reasons: list[str] = []
    metadata: dict[str, Any] = {}

    now_ist = _as_ist(context.now)
    now_time = now_ist.time()
    if now_time < cfg.entry_start_time:
        reasons.append("outside_entry_window:before_start")
    if now_time > cfg.entry_end_time:
        reasons.append("outside_entry_window:after_end")
    if now_time >= cfg.hard_squareoff_time:
        reasons.append("after_hard_squareoff_time")

    _check_kill_switch(context, cfg, reasons)

    structure = _normalise_structure(context.structure)
    if structure is None:
        reasons.append("unsupported_structure")

    option_type = _normalise_option_type(context)
    if option_type != "CE":
        reasons.append("unsupported_option_type")

    quote = _normalise_quote(context.quote, cfg, now_ist, reasons, metadata)

    ml_score = _as_float(context.ml_score)
    if ml_score is None:
        reasons.append("missing_ml_score")

    vix = _as_float(context.vix)
    if vix is None:
        reasons.append("missing_vix")
    elif vix > cfg.max_entry_vix:
        reasons.append("vix_above_entry_max")

    premium = _as_float(context.premium_received)
    if premium is None or premium <= 0:
        reasons.append("invalid_premium_received")

    predicted_mae = _as_float(context.predicted_mae_premium)
    if predicted_mae is None:
        reasons.append("missing_predicted_mae")
    elif premium is not None and premium > 0:
        mae_limit = premium * cfg.max_mae_to_premium
        metadata["mae_limit"] = mae_limit
        if predicted_mae > mae_limit:
            reasons.append("predicted_mae_above_limit")

    max_loss = _as_float(context.max_loss_rupees)
    if max_loss is None or max_loss <= 0:
        reasons.append("invalid_max_loss_rupees")

    if structure is OptionSellStructure.BEAR_CALL_SPREAD:
        if ml_score is not None and ml_score < cfg.min_spread_ml_score:
            reasons.append("ml_score_below_spread_min")
        if max_loss is not None and max_loss > cfg.max_spread_loss_rupees:
            reasons.append("spread_loss_above_limit")
    elif structure is OptionSellStructure.NAKED_SHORT_CE:
        hard_reasons = list(reasons)
        spread_reasons = _naked_to_spread_reasons(
            cfg=cfg,
            ml_score=ml_score,
            vix=vix,
        )
        if spread_reasons:
            reasons.extend(spread_reasons)
            if not hard_reasons:
                return OptionSellGuardResult.reject(
                    reasons,
                    required_structure=OptionSellStructure.BEAR_CALL_SPREAD,
                    metadata=metadata,
                )
        if max_loss is not None and max_loss > cfg.max_naked_loss_rupees:
            reasons.append("naked_loss_above_limit")

    if reasons:
        return OptionSellGuardResult.reject(reasons, metadata=metadata)

    return OptionSellGuardResult(
        allowed=True,
        decision=OptionSellGuardDecision.ALLOW,
        reasons=("allowed",),
        metadata={
            **metadata,
            "structure": structure.value if structure else None,
            "quote_snapshot_ts": quote.snapshot_ts.isoformat() if quote else None,
        },
    )


def _check_kill_switch(
    context: OptionSellGuardContext,
    cfg: OptionSellGuardConfig,
    reasons: list[str],
) -> None:
    manager = context.kill_switch_manager
    if manager is None:
        if cfg.require_kill_switch_manager:
            reasons.append("missing_kill_switch_manager")
        return
    try:
        if manager.is_tripped_for_scope(
            tenant_id=context.tenant_id,
            account_id=context.account_id,
            strategy_id=context.strategy_id,
        ):
            reasons.append("kill_switch_tripped")
    except Exception:
        reasons.append("kill_switch_check_failed")


def _normalise_quote(
    quote: OptionQuote | None,
    cfg: OptionSellGuardConfig,
    now_ist: datetime,
    reasons: list[str],
    metadata: dict[str, Any],
) -> OptionQuote | None:
    if quote is None:
        reasons.append("missing_option_quote")
        return None

    normalised = quote.normalized()
    flags = quality_flags_for_quote(
        normalised,
        max_source_lag_seconds=cfg.max_quote_age_seconds,
    )
    if flags:
        metadata["quote_quality_flags"] = flags
        reasons.extend(f"quote_quality:{name}" for name in sorted(flags))

    snapshot_ts = _as_ist(normalised.snapshot_ts)
    age_seconds = (now_ist - snapshot_ts).total_seconds()
    if age_seconds > cfg.max_quote_age_seconds:
        metadata["quote_age_seconds"] = int(age_seconds)
        reasons.append("stale_option_quote")
    elif age_seconds < -cfg.max_quote_age_seconds:
        metadata["quote_age_seconds"] = int(age_seconds)
        reasons.append("future_option_quote")

    return normalised


def _naked_to_spread_reasons(
    *,
    cfg: OptionSellGuardConfig,
    ml_score: float | None,
    vix: float | None,
) -> list[str]:
    reasons: list[str] = []
    if not cfg.allow_naked:
        reasons.append("naked_disabled")
    if ml_score is not None and ml_score < cfg.min_naked_ml_score:
        reasons.append("naked_ml_score_below_min")
    if vix is not None and vix >= cfg.naked_max_vix:
        reasons.append("naked_vix_above_max")
    return reasons


def _normalise_structure(value: OptionSellStructure | str) -> OptionSellStructure | None:
    if isinstance(value, OptionSellStructure):
        return value
    try:
        return OptionSellStructure(str(value).strip().upper())
    except Exception:
        return None


def _normalise_option_type(context: OptionSellGuardContext) -> str:
    if context.quote is not None and context.quote.option_type:
        return str(context.quote.option_type).strip().upper()
    return str(context.option_type).strip().upper()


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(IST)


__all__ = [
    "OptionSellGuardConfig",
    "OptionSellGuardContext",
    "OptionSellGuardDecision",
    "OptionSellGuardResult",
    "OptionSellStructure",
    "evaluate_option_sell_guard",
]
