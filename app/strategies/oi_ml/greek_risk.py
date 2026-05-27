"""Greek-risk controls for the OI/ML CE seller.

The helpers here are pure policy logic. They do not submit, modify, or cancel
orders; callers use the returned assessment to reject, force spreads, scale
size, or tighten exits through their existing order paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app.data.option_chain_provider import OptionQuote


@dataclass(frozen=True)
class OiMlGreekRiskConfig:
    """Pre-trade and post-entry Greek risk policy."""

    enabled: bool = True
    require_greeks: bool = True
    require_oi_wall: bool = True
    target_abs_delta: float = 0.20
    min_abs_delta: float = 0.05
    max_abs_delta: float = 0.35
    max_abs_gamma: float = 0.0030
    max_abs_vega: float | None = None
    force_spread_abs_gamma: float = 0.0015
    force_spread_abs_vega: float = 8.0
    size_down_abs_delta: float = 0.25
    size_down_abs_gamma: float = 0.0012
    size_down_abs_vega: float = 7.0
    size_down_lot_multiplier: float = 0.50
    exit_abs_delta: float = 0.45
    exit_abs_gamma: float = 0.0040
    exit_iv_expansion_pct: float = 0.25
    tighten_abs_delta: float = 0.30
    tighten_abs_gamma: float = 0.0020
    tighten_iv_expansion_pct: float = 0.15
    tightened_stop_loss_mult_credit: float = 1.25


@dataclass(frozen=True)
class OiMlGreekRiskAssessment:
    """Greek-risk result for one candidate quote."""

    allowed: bool
    reasons: tuple[str, ...]
    force_spread: bool
    lot_multiplier: float
    metadata: Mapping[str, Any]


def assess_candidate_greek_risk(
    quote: OptionQuote,
    *,
    features: Mapping[str, Any],
    config: OiMlGreekRiskConfig | None = None,
) -> OiMlGreekRiskAssessment:
    """Assess whether one option candidate is safe enough to consider."""

    cfg = config or OiMlGreekRiskConfig()
    if not cfg.enabled:
        return OiMlGreekRiskAssessment(
            allowed=True,
            reasons=(),
            force_spread=False,
            lot_multiplier=1.0,
            metadata={"enabled": False},
        )

    row = quote.normalized()
    abs_delta = abs_delta_value(row.delta)
    abs_gamma = abs_decimal_value(row.gamma)
    abs_vega = abs_decimal_value(row.vega)
    theta = decimal_value(row.theta)
    iv = decimal_value(row.iv)

    reasons: list[str] = []
    if cfg.require_oi_wall and not bool(features.get("oi_wall_present")):
        reasons.append("oi_wall_missing")

    if abs_delta is None:
        if cfg.require_greeks:
            reasons.append("missing_greek_delta")
    elif abs_delta < float(cfg.min_abs_delta):
        reasons.append("delta_below_min")
    elif abs_delta > float(cfg.max_abs_delta):
        reasons.append("delta_above_max")

    if abs_gamma is None:
        if cfg.require_greeks:
            reasons.append("missing_greek_gamma")
    elif abs_gamma > float(cfg.max_abs_gamma):
        reasons.append("gamma_above_max")

    if abs_vega is None:
        if cfg.require_greeks:
            reasons.append("missing_greek_vega")
    elif cfg.max_abs_vega is not None and abs_vega > float(cfg.max_abs_vega):
        reasons.append("vega_above_max")

    force_spread = (
        _gte(abs_gamma, cfg.force_spread_abs_gamma)
        or _gte(abs_vega, cfg.force_spread_abs_vega)
    )
    lot_multiplier = 1.0
    if (
        _gte(abs_delta, cfg.size_down_abs_delta)
        or _gte(abs_gamma, cfg.size_down_abs_gamma)
        or _gte(abs_vega, cfg.size_down_abs_vega)
    ):
        lot_multiplier = _bounded_multiplier(cfg.size_down_lot_multiplier)

    metadata = {
        "enabled": True,
        "abs_delta": abs_delta,
        "abs_gamma": abs_gamma,
        "abs_vega": abs_vega,
        "theta": theta,
        "iv": iv,
        "target_abs_delta": float(cfg.target_abs_delta),
        "delta_distance_from_target": (
            abs(abs_delta - float(cfg.target_abs_delta))
            if abs_delta is not None
            else None
        ),
        "oi_wall_present": bool(features.get("oi_wall_present")),
        "oi_wall_strike": features.get("oi_wall_strike"),
        "oi_wall_multiple": features.get("oi_wall_multiple"),
        "force_spread": force_spread,
        "lot_multiplier": lot_multiplier,
        "stress_score": _stress_score(
            abs_delta=abs_delta,
            abs_gamma=abs_gamma,
            abs_vega=abs_vega,
            config=cfg,
        ),
        "reasons": list(reasons),
    }
    return OiMlGreekRiskAssessment(
        allowed=not reasons,
        reasons=tuple(reasons),
        force_spread=force_spread,
        lot_multiplier=lot_multiplier,
        metadata=metadata,
    )


def greek_metadata_from_quote(quote: OptionQuote | None) -> dict[str, float | None]:
    if quote is None:
        return {
            "delta": None,
            "abs_delta": None,
            "gamma": None,
            "abs_gamma": None,
            "theta": None,
            "vega": None,
            "abs_vega": None,
            "iv": None,
        }
    row = quote.normalized()
    return {
        "delta": decimal_value(row.delta),
        "abs_delta": abs_delta_value(row.delta),
        "gamma": decimal_value(row.gamma),
        "abs_gamma": abs_decimal_value(row.gamma),
        "theta": decimal_value(row.theta),
        "vega": decimal_value(row.vega),
        "abs_vega": abs_decimal_value(row.vega),
        "iv": decimal_value(row.iv),
    }


def decimal_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def abs_decimal_value(value: Any) -> float | None:
    parsed = decimal_value(value)
    return abs(parsed) if parsed is not None else None


def abs_delta_value(value: Any) -> float | None:
    parsed = abs_decimal_value(value)
    if parsed is None:
        return None
    if parsed > 1.0 and parsed <= 100.0:
        return parsed / 100.0
    return parsed


def _gte(value: float | None, threshold: float) -> bool:
    return value is not None and value >= float(threshold)


def _bounded_multiplier(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1.0
    if parsed <= 0:
        return 1.0
    return min(1.0, parsed)


def _stress_score(
    *,
    abs_delta: float | None,
    abs_gamma: float | None,
    abs_vega: float | None,
    config: OiMlGreekRiskConfig,
) -> float:
    scores = [
        _ratio(abs_delta, config.max_abs_delta),
        _ratio(abs_gamma, config.max_abs_gamma),
        _ratio(abs_vega, config.force_spread_abs_vega),
    ]
    present_scores = [score for score in scores if score is not None]
    return max(present_scores) if present_scores else 0.0


def _ratio(value: float | None, denominator: float | None) -> float | None:
    if value is None or denominator in (None, 0):
        return None
    return float(value) / float(denominator)


__all__ = [
    "OiMlGreekRiskAssessment",
    "OiMlGreekRiskConfig",
    "abs_decimal_value",
    "abs_delta_value",
    "assess_candidate_greek_risk",
    "decimal_value",
    "greek_metadata_from_quote",
]
