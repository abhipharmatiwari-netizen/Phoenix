"""Replay optimizer and recommendation logic."""

from __future__ import annotations

import itertools
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from scripts.replay.execution_models import ExecutionConfig
from scripts.replay.pnl_tracker import PnLTracker, StrategyMetrics
from scripts.replay.recommendations import load_strategy_defaults
from scripts.replay.replay_engine import VALID_COMBINATIONS, run_single_replay

logger = logging.getLogger(__name__)

DEFAULT_PARAMS: Dict[str, Dict[str, Any]] = {
    "ema20_strategy": {
        "sl_pct": 0.30,
        "tp_pct": 0.30,
        "ema_period": 20,
        "require_rsi_falling": True,
        "use_adx_filter": False,
        "min_adx": 18.0,
        "signal_timeframe": 300,
    },
    "put_momentum_scalper": {
        "option_sl_pct": 0.25,
        "partial_tp_r": 1.0,
        "final_tp_r": 1.5,
        "rsi_min": 25.0,
        "rsi_max": 45.0,
        "min_atr_ratio": 0.0015,
        "max_bars_in_trade": 8,
        "lookback_breakdown_bars": 10,
        "rsi_falling_bars_required": 2,
    },
    "exclusive_nifty_ce_buy": {
        "sl_atr": 2.2,
        "tp_atr": 2.5,
        "rsi_min": 58,
        "rsi_max": 72,
        "ema_atr_buffer": 0.05,
        "macd_hist_min": 0.30,
        "min_adx": 20.0,
        "min_di_spread": 5.0,
        "vol_quantile": 0.45,
        "ema_fail_bars": 3,
        "timeframe_seconds": 30,
    },
}

EMA20_GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "default": {
        "ema_period": [20, 30],
        "sl_pct": [0.20, 0.25, 0.30],
        "tp_pct": [0.20, 0.30, 0.40],
        "require_rsi_falling": [True, False],
        "use_adx_filter": [False, True],
        "min_adx": [16.0, 18.0, 22.0],
    },
    "NG_FUT": {
        "ema_period": [8, 13, 20],
        "sl_pct": [0.15, 0.20, 0.25],
        "tp_pct": [0.20, 0.30, 0.40],
        "min_atr": [0.8, 1.0, 1.2, 1.5],
        "require_rsi_falling": [False, True],
        "use_adx_filter": [False, True],
    },
}

PUT_MOM_PARAM_GRID: Dict[str, List[Any]] = {
    "option_sl_pct": [0.20, 0.25, 0.30],
    "partial_tp_r": [0.8, 1.0, 1.2],
    "final_tp_r": [1.2, 1.5, 2.0],
    "rsi_min": [20.0, 25.0, 30.0],
    "rsi_max": [40.0, 45.0, 50.0],
    "min_atr_ratio": [0.0010, 0.0015, 0.0020],
    "lookback_breakdown_bars": [8, 10, 12],
    "max_bars_in_trade": [6, 8, 10, 12],
}

EXCLUSIVE_CE_PARAM_GRID: Dict[str, List[Any]] = {
    "sl_atr": [1.8, 2.2, 2.5],
    "tp_atr": [2.0, 2.5, 3.0],
    "rsi_min": [55, 58, 62],
    "rsi_max": [68, 72, 76],
    "ema_atr_buffer": [0.0, 0.05, 0.10],
    "macd_hist_min": [0.15, 0.30, 0.50],
    "min_adx": [14.0, 16.0, 20.0],
    "min_di_spread": [2.0, 5.0, 8.0],
}


@dataclass
class OptimizationResult:
    """One evaluated parameter combination."""

    params: Dict[str, Any]
    metrics: StrategyMetrics
    score: float = 0.0
    is_default: bool = False
    sample_ok: bool = True
    walk_forward_score: Optional[float] = None
    stability_score: Optional[float] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class WalkForwardFold:
    """A single train/test fold."""

    train_start: date
    train_end: date
    test_start: date
    test_end: date


@dataclass
class ParameterRecommendation:
    """A recommended parameter change with supporting evidence."""

    parameter: str
    current_value: Any
    recommended_value: Any
    improvement_pct: float
    confidence: str
    evidence: str
    production_safe: bool
    warnings: List[str] = field(default_factory=list)


def strategy_grid(strategy_id: str, underlying_key: str) -> Dict[str, List[Any]]:
    if strategy_id == "ema20_strategy":
        base = dict(EMA20_GRIDS["default"])
        base.update(EMA20_GRIDS.get(underlying_key, {}))
        return base
    if strategy_id == "put_momentum_scalper":
        return dict(PUT_MOM_PARAM_GRID)
    if strategy_id == "exclusive_nifty_ce_buy":
        return dict(EXCLUSIVE_CE_PARAM_GRID)
    return {}


def compute_robustness_score(m: StrategyMetrics, *, min_trade_count: int = 5) -> float:
    """Score a parameter set for replay robustness, not just raw PnL."""

    if m.total_trades < min_trade_count:
        return 0.0

    pnl_score = _cap_positive(m.net_pnl / 5000.0)
    expectancy_score = _cap_positive(m.expectancy / 200.0)
    win_rate_score = _cap_positive(m.win_rate)
    pf_score = _cap_positive((m.profit_factor - 1.0) / 2.0) if m.profit_factor > 1.0 else 0.0
    sharpe_score = _cap_positive((m.sharpe_ratio or 0.0) / 2.5)
    drawdown_score = max(0.0, 1.0 - _cap_positive(m.max_drawdown / max(abs(m.net_pnl), 1000.0)))
    trade_count_score = _cap_positive(m.total_trades / 25.0)

    raw = (
        0.24 * pnl_score
        + 0.18 * expectancy_score
        + 0.12 * win_rate_score
        + 0.18 * pf_score
        + 0.14 * sharpe_score
        + 0.08 * drawdown_score
        + 0.06 * trade_count_score
    )
    return round(raw * 100.0, 2)


def run_grid_search(
    dsn: str,
    strategy_id: str,
    underlying_key: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    max_combos: int = 500,
    table: str = "indicator_bars",
    *,
    chunk_size: int = 5000,
    execution: Optional[ExecutionConfig] = None,
    min_trade_count: int = 5,
    strategy_env_path: Optional[str] = None,
) -> List[OptimizationResult]:
    """Run replay grid search and sort by robustness."""

    if underlying_key not in VALID_COMBINATIONS.get(strategy_id, []):
        logger.warning("Skipping invalid combination: %s + %s", strategy_id, underlying_key)
        return []

    defaults = load_strategy_defaults(
        strategy_id=strategy_id,
        underlying_key=underlying_key,
        fallback=DEFAULT_PARAMS.get(strategy_id, {}),
        config_path=strategy_env_path,
    )
    grid = strategy_grid(strategy_id, underlying_key)
    combos = _generate_grid_combos(grid, max_combos=max_combos)
    baseline_combo = {k: defaults.get(k, values[0]) for k, values in grid.items()}
    if baseline_combo not in combos:
        combos.insert(0, baseline_combo)

    results: List[OptimizationResult] = []
    for combo in combos:
        merged = {**defaults, **combo}
        recorder = run_single_replay(
            dsn=dsn,
            strategy_id=strategy_id,
            underlying_key=underlying_key,
            params=merged,
            start_date=start_date,
            end_date=end_date,
            table=table,
            chunk_size=chunk_size,
            execution=execution,
        )
        tracker = PnLTracker()
        tracker.process_fills(recorder.fills)
        metrics = tracker.compute_metrics(
            strategy_id,
            underlying_key,
            replay_first_date=getattr(recorder, "replay_first_date", None),
            replay_last_date=getattr(recorder, "replay_last_date", None),
            replay_trading_days=getattr(recorder, "replay_trading_days", None),
        )
        warnings: List[str] = []
        sample_ok = metrics.total_trades >= int(min_trade_count)
        if not sample_ok:
            warnings.append(
                f"Only {metrics.total_trades} trades captured; below min_trade_count={min_trade_count}."
            )
        result = OptimizationResult(
            params=combo,
            metrics=metrics,
            score=compute_robustness_score(metrics, min_trade_count=min_trade_count),
            is_default=all(merged.get(key) == defaults.get(key) for key in grid),
            sample_ok=sample_ok,
            warnings=warnings,
        )
        results.append(result)

    results.sort(
        key=lambda item: (
            float(item.score),
            float(item.metrics.net_pnl),
            float(item.metrics.profit_factor if item.metrics.profit_factor != float("inf") else 9999.0),
        ),
        reverse=True,
    )
    _apply_stability_scores(results)
    return results


def walk_forward_folds(
    start_date: date,
    end_date: date,
    *,
    train_days: int = 60,
    test_days: int = 20,
    step_days: int = 20,
) -> List[WalkForwardFold]:
    """Generate non-leaking rolling train/test folds."""

    folds: List[WalkForwardFold] = []
    current = start_date
    while current + timedelta(days=train_days + test_days) <= end_date:
        train_end = current + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        folds.append(
            WalkForwardFold(
                train_start=current,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        current += timedelta(days=step_days)
    return folds


def walk_forward_validate(
    dsn: str,
    strategy_id: str,
    underlying_key: str,
    candidate_params: Dict[str, Any],
    start_date: date,
    end_date: date,
    table: str = "indicator_bars",
    *,
    chunk_size: int = 5000,
    execution: Optional[ExecutionConfig] = None,
    train_days: int = 60,
    test_days: int = 20,
    step_days: int = 20,
) -> Tuple[float, List[StrategyMetrics]]:
    """Evaluate a candidate only on out-of-sample windows."""

    folds = walk_forward_folds(
        start_date,
        end_date,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
    )
    if not folds:
        return 0.0, []

    fold_metrics: List[StrategyMetrics] = []
    fold_scores: List[float] = []
    for fold in folds:
        recorder = run_single_replay(
            dsn=dsn,
            strategy_id=strategy_id,
            underlying_key=underlying_key,
            params=candidate_params,
            start_date=fold.test_start,
            end_date=fold.test_end,
            table=table,
            chunk_size=chunk_size,
            execution=execution,
        )
        tracker = PnLTracker()
        tracker.process_fills(recorder.fills)
        metrics = tracker.compute_metrics(
            strategy_id,
            underlying_key,
            replay_first_date=getattr(recorder, "replay_first_date", None),
            replay_last_date=getattr(recorder, "replay_last_date", None),
            replay_trading_days=getattr(recorder, "replay_trading_days", None),
        )
        fold_metrics.append(metrics)
        fold_scores.append(compute_robustness_score(metrics))

    return (sum(fold_scores) / len(fold_scores) if fold_scores else 0.0), fold_metrics


def analyze_parameter_sensitivity(
    results: Sequence[OptimizationResult],
    *,
    top_n: int = 20,
) -> Dict[str, List[Dict[str, Any]]]:
    """Summarize score sensitivity by parameter value."""

    rows = list(results[: max(1, int(top_n))])
    buckets: Dict[str, Dict[Any, List[float]]] = {}
    for result in rows:
        for key, value in result.params.items():
            buckets.setdefault(str(key), {}).setdefault(value, []).append(float(result.score))

    summary: Dict[str, List[Dict[str, Any]]] = {}
    for key, values in buckets.items():
        entries = []
        for value, scores in values.items():
            entries.append(
                {
                    "value": value,
                    "count": len(scores),
                    "avg_score": round(sum(scores) / len(scores), 2),
                    "median_score": round(statistics.median(scores), 2),
                }
            )
        entries.sort(key=lambda row: (-float(row["avg_score"]), -int(row["count"])))
        summary[key] = entries
    return summary


def generate_recommendations(
    *,
    default_result: OptimizationResult,
    top_results: Sequence[OptimizationResult],
    strategy_id: str,
    walk_forward_score: Optional[float] = None,
    current_params: Optional[Mapping[str, Any]] = None,
) -> List[ParameterRecommendation]:
    """Recommend only stable parameter changes with enough evidence."""

    defaults = dict(current_params or DEFAULT_PARAMS.get(strategy_id, {}))
    recommendations: List[ParameterRecommendation] = []
    if not top_results or default_result.score <= 0:
        return recommendations

    top5 = list(top_results[:5])
    for param, current_value in defaults.items():
        candidate_rows = [item for item in top5 if param in item.params]
        if not candidate_rows:
            continue
        counts: Dict[Any, int] = {}
        for item in candidate_rows:
            counts[item.params.get(param)] = counts.get(item.params.get(param), 0) + 1
        best_value, best_count = max(counts.items(), key=lambda item: item[1])
        if best_value == current_value:
            continue

        matching_scores = [row.score for row in candidate_rows if row.params.get(param) == best_value]
        best_score = max(matching_scores) if matching_scores else 0.0
        improvement = ((best_score - default_result.score) / default_result.score) * 100.0
        if improvement < 5.0:
            continue

        consistency = best_count / max(1, len(candidate_rows))
        confidence = "LOW"
        if consistency >= 0.8:
            confidence = "HIGH"
        elif consistency >= 0.6:
            confidence = "MEDIUM"

        warnings: List[str] = []
        production_safe = confidence in {"HIGH", "MEDIUM"}
        if walk_forward_score is not None and walk_forward_score < 30.0:
            production_safe = False
            warnings.append("Walk-forward score stayed weak out of sample.")
        if not all(row.sample_ok for row in candidate_rows):
            production_safe = False
            warnings.append("Top candidates include low-trade samples.")

        evidence = (
            f"Top-5 consistency: {best_count}/{len(candidate_rows)} | "
            f"Score improvement: {improvement:.1f}% | Default score: {default_result.score:.1f}"
        )
        if walk_forward_score is not None:
            evidence += f" | Walk-forward score: {walk_forward_score:.1f}"

        recommendations.append(
            ParameterRecommendation(
                parameter=param,
                current_value=current_value,
                recommended_value=best_value,
                improvement_pct=round(improvement, 1),
                confidence=confidence,
                evidence=evidence,
                production_safe=production_safe,
                warnings=warnings,
            )
        )

    recommendations.sort(key=lambda item: item.improvement_pct, reverse=True)
    return recommendations


def _generate_grid_combos(grid: Mapping[str, Sequence[Any]], *, max_combos: int) -> List[Dict[str, Any]]:
    keys = sorted(grid.keys())
    values = [list(grid[key]) for key in keys]
    combos: List[Dict[str, Any]] = []
    for row in itertools.product(*values):
        combos.append(dict(zip(keys, row)))
        if len(combos) >= max(1, int(max_combos)):
            break
    return combos


def _apply_stability_scores(results: List[OptimizationResult]) -> None:
    top = results[:10]
    if not top:
        return
    sensitivity = analyze_parameter_sensitivity(top, top_n=min(10, len(top)))
    for result in results:
        score_parts = []
        for key, value in result.params.items():
            for row in sensitivity.get(str(key), []):
                if row.get("value") == value:
                    score_parts.append(float(row.get("count", 0)))
                    break
        if score_parts:
            result.stability_score = round(sum(score_parts) / len(score_parts), 2)


def _cap_positive(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


__all__ = [
    "DEFAULT_PARAMS",
    "OptimizationResult",
    "ParameterRecommendation",
    "WalkForwardFold",
    "analyze_parameter_sensitivity",
    "compute_robustness_score",
    "generate_recommendations",
    "run_grid_search",
    "strategy_grid",
    "walk_forward_folds",
    "walk_forward_validate",
]
