#!/usr/bin/env python3
"""Phoenix v9 Strategy Replay CLI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.orders.replay_context import isolated_replay_flag
from app.strategies.ema20_strategy import resolve_ema20_lots_config
from scripts.replay.diagnostics import build_trade_analysis
from scripts.replay.execution_models import ExecutionConfig, normalize_execution_config
from scripts.replay.optimizer import (
    DEFAULT_PARAMS,
    OptimizationResult,
    analyze_parameter_sensitivity,
    generate_recommendations,
    run_grid_search,
    walk_forward_validate,
)
from scripts.replay.pnl_tracker import PnLTracker, StrategyMetrics
from scripts.replay.recommendations import DEFAULT_STRATEGY_ENV_PATH, load_strategy_defaults
from scripts.replay.replay_engine import (
    UNDERLYING_MAP,
    VALID_COMBINATIONS,
    run_single_replay,
)
from scripts.replay.report import (
    format_assumptions,
    format_invalid_combinations,
    format_metrics_table,
    format_recommendations,
    write_full_report,
)

logger = logging.getLogger("replay")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phoenix v9 Strategy Replay Engine")
    parser.add_argument("--dsn", default=os.environ.get("REPLAY_PG_DSN", ""), help="Postgres DSN")
    parser.add_argument("--table", default=os.environ.get("REPLAY_TABLE", "indicator_bars"), help="Replay table")
    parser.add_argument("--mode", choices=["evaluate", "optimize", "both"], default="both")
    parser.add_argument("--strategy", choices=list(VALID_COMBINATIONS.keys()))
    parser.add_argument("--underlying", choices=list(UNDERLYING_MAP.keys()))
    parser.add_argument("--start-date", type=parse_date)
    parser.add_argument("--end-date", type=parse_date)
    parser.add_argument("--max-combos", type=int, default=200)
    parser.add_argument("--output-dir", default="replay_output")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=20)
    parser.add_argument("--step-days", type=int, default=20)
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--strategy-env", default=str(DEFAULT_STRATEGY_ENV_PATH))
    parser.add_argument("--fill-mode", choices=["bar_close_fill", "next_bar_open_fill"], default="bar_close_fill")
    parser.add_argument("--tick-model", choices=["open_close", "ohlc"], default="ohlc")
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--fixed-slippage", type=float, default=0.0)
    parser.add_argument("--spread-bps", type=float, default=0.0)
    parser.add_argument("--latency-bars", type=int, default=0)
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def get_combinations(strategy: Optional[str], underlying: Optional[str]) -> List[tuple[str, str]]:
    combos: List[tuple[str, str]] = []
    strategies = [strategy] if strategy else list(VALID_COMBINATIONS.keys())
    for strategy_id in strategies:
        underlyings = [underlying] if underlying else VALID_COMBINATIONS.get(strategy_id, [])
        for underlying_key in underlyings:
            if underlying_key in VALID_COMBINATIONS.get(strategy_id, []):
                combos.append((strategy_id, underlying_key))
    return combos


def _qty_resolution_summary(strategy_id: str, underlying_key: str) -> Dict[str, Any]:
    if strategy_id != "ema20_strategy":
        return {"kind": "n/a", "lots": None, "selected_key": None, "defaulted": False}
    resolution = resolve_ema20_lots_config(
        underlying_key=underlying_key,
        env=dict(os.environ),
        params=DEFAULT_PARAMS.get(strategy_id, {}),
    )
    return {
        "kind": "ema20",
        "lots": resolution.lots,
        "selected_key": resolution.selected_key or "default",
        "defaulted": resolution.defaulted,
    }


def log_replay_startup_summary(*, mode: str, combos: List[tuple[str, str]], table: str, execution: ExecutionConfig) -> None:
    logger.info(
        "Replay startup summary | mode=%s table=%s execution_mode=isolated_local fill_mode=%s tick_model=%s combinations=%d",
        mode,
        table,
        execution.fill_mode,
        execution.tick_model,
        len(combos),
    )
    for strategy_id, underlying_key in combos:
        qty_summary = _qty_resolution_summary(strategy_id, underlying_key)
        logger.info(
            "Replay combo summary | strategy=%s underlying=%s qty_kind=%s lots=%s selected_key=%s defaulted=%s",
            strategy_id,
            underlying_key,
            qty_summary["kind"],
            qty_summary["lots"],
            qty_summary["selected_key"],
            qty_summary["defaulted"],
        )


def resolved_defaults(strategy_id: str, underlying_key: str, strategy_env_path: str) -> Dict[str, Any]:
    return load_strategy_defaults(
        strategy_id=strategy_id,
        underlying_key=underlying_key,
        fallback=DEFAULT_PARAMS.get(strategy_id, {}),
        config_path=strategy_env_path,
    )


def run_evaluate(
    dsn: str,
    combos: List[tuple[str, str]],
    start_date: Optional[date],
    end_date: Optional[date],
    table: str,
    *,
    strategy_env_path: str = str(DEFAULT_STRATEGY_ENV_PATH),
    execution: Optional[ExecutionConfig] = None,
    chunk_size: int = 5000,
    combo_analyses: Optional[Dict[str, Dict[str, Any]]] = None,
) -> tuple:
    execution_cfg = normalize_execution_config(execution)
    all_metrics: Dict[str, StrategyMetrics] = {}
    all_trackers: Dict[str, PnLTracker] = {}
    bars_loaded: Dict[str, int] = {}
    gate_summaries: Dict[str, List[Dict[str, Any]]] = {}
    missing_indicators: Dict[str, Dict[str, int]] = {}

    for strategy_id, underlying_key in combos:
        key = f"{strategy_id}/{underlying_key}"
        defaults = resolved_defaults(strategy_id, underlying_key, strategy_env_path)
        recorder = run_single_replay(
            dsn=dsn,
            strategy_id=strategy_id,
            underlying_key=underlying_key,
            params=defaults,
            start_date=start_date,
            end_date=end_date,
            table=table,
            chunk_size=chunk_size,
            execution=execution_cfg,
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
        bars_loaded[key] = int(getattr(recorder, "db_bar_count", 0))
        gate_summaries[key] = recorder.gate_summary_rows()
        all_metrics[key] = metrics
        all_trackers[key] = tracker
        if combo_analyses is not None:
            combo_analyses[key] = build_trade_analysis(
                combo_key=key,
                metrics=metrics,
                trades=tracker.trades,
                gate_rows=gate_summaries[key],
                data_profile=getattr(recorder, "data_profile", {}),
                indicator_snapshot=(getattr(recorder, "data_profile", {}) or {}).get("strategy_indicator_snapshot"),
                finalization_events=getattr(recorder, "finalization_events", []),
            )
        print(format_metrics_table(metrics))

    return all_metrics, all_trackers, bars_loaded, gate_summaries, missing_indicators


def run_optimize(
    dsn: str,
    combos: List[tuple[str, str]],
    start_date: Optional[date],
    end_date: Optional[date],
    table: str,
    max_combos: int,
    walk_forward: bool,
    *,
    strategy_env_path: str = str(DEFAULT_STRATEGY_ENV_PATH),
    execution: Optional[ExecutionConfig] = None,
    chunk_size: int = 5000,
    train_days: int = 60,
    test_days: int = 20,
    step_days: int = 20,
    parameter_sensitivity: Optional[Dict[str, Dict[str, Any]]] = None,
    recommendation_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
) -> tuple:
    execution_cfg = normalize_execution_config(execution)
    all_opt_results: Dict[str, List[OptimizationResult]] = {}
    all_recommendations: Dict[str, List] = {}

    for strategy_id, underlying_key in combos:
        key = f"{strategy_id}/{underlying_key}"
        defaults = resolved_defaults(strategy_id, underlying_key, strategy_env_path)
        results = run_grid_search(
            dsn=dsn,
            strategy_id=strategy_id,
            underlying_key=underlying_key,
            start_date=start_date,
            end_date=end_date,
            max_combos=max_combos,
            table=table,
            chunk_size=chunk_size,
            execution=execution_cfg,
            strategy_env_path=strategy_env_path,
        )
        all_opt_results[key] = results
        parameter_sensitivity and parameter_sensitivity.setdefault(key, analyze_parameter_sensitivity(results))
        if not results:
            all_recommendations[key] = []
            continue

        default_result = next((row for row in results if row.is_default), results[-1])
        best_result = results[0]
        walk_forward_score: Optional[float] = None
        if walk_forward and start_date and end_date:
            walk_forward_score, fold_metrics = walk_forward_validate(
                dsn=dsn,
                strategy_id=strategy_id,
                underlying_key=underlying_key,
                candidate_params={**defaults, **best_result.params},
                start_date=start_date,
                end_date=end_date,
                table=table,
                chunk_size=chunk_size,
                execution=execution_cfg,
                train_days=train_days,
                test_days=test_days,
                step_days=step_days,
            )
            best_result.walk_forward_score = walk_forward_score

        recs = generate_recommendations(
            default_result=default_result,
            top_results=results[:10],
            strategy_id=strategy_id,
            walk_forward_score=walk_forward_score,
            current_params=defaults,
        )
        all_recommendations[key] = recs
        if recommendation_payloads is not None:
            recommendation_payloads[key] = {
                "strategy_id": strategy_id,
                "underlying_key": underlying_key,
                "underlying_label": UNDERLYING_MAP[underlying_key]["underlying_label"],
                "current_config_params": defaults,
                "recommended_params": {**defaults, **best_result.params} if recs else {},
                "evidence": {
                    "default_score": default_result.score,
                    "best_score": best_result.score,
                    "walk_forward_score": walk_forward_score,
                },
                "warnings": [warning for rec in recs for warning in rec.warnings],
            }
        print(f"\n--- Optimization: {key} ---")
        print(f"  Default score: {default_result.score:.1f}")
        print(f"  Best score:    {best_result.score:.1f}")
        print(f"  Best params:   {best_result.params}")
        print(format_recommendations(recs))

    return all_opt_results, all_recommendations


def build_execution_config(args: argparse.Namespace) -> ExecutionConfig:
    return normalize_execution_config(
        fill_mode=args.fill_mode,
        tick_model=args.tick_model,
        slippage_bps=args.slippage_bps,
        fixed_slippage=args.fixed_slippage,
        spread_bps=args.spread_bps,
        latency_bars=args.latency_bars,
    )


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if not args.dsn:
        print("ERROR: --dsn or REPLAY_PG_DSN is required", file=sys.stderr)
        sys.exit(1)
    combos = get_combinations(args.strategy, args.underlying)
    if not combos:
        print("No valid strategy/underlying combinations to evaluate.")
        print(format_invalid_combinations())
        sys.exit(0)

    execution = build_execution_config(args)
    combo_analyses: Dict[str, Dict[str, Any]] = {}
    parameter_sensitivity: Dict[str, Dict[str, Any]] = {}
    recommendation_payloads: Dict[str, Dict[str, Any]] = {}

    all_metrics: Dict[str, StrategyMetrics] = {}
    all_trackers: Dict[str, PnLTracker] = {}
    bars_loaded: Dict[str, int] = {}
    gate_summaries: Dict[str, List[Dict[str, Any]]] = {}
    missing_indicators: Dict[str, Dict[str, int]] = {}
    all_opt_results: Dict[str, List[OptimizationResult]] = {}
    all_recommendations: Dict[str, List] = {}

    with isolated_replay_flag(True):
        log_replay_startup_summary(mode=args.mode, combos=combos, table=args.table, execution=execution)
        if args.mode in {"evaluate", "both"}:
            metrics, trackers, loaded, gates, missing = run_evaluate(
                dsn=args.dsn,
                combos=combos,
                start_date=args.start_date,
                end_date=args.end_date,
                table=args.table,
                strategy_env_path=args.strategy_env,
                execution=execution,
                chunk_size=args.chunk_size,
                combo_analyses=combo_analyses,
            )
            all_metrics.update(metrics)
            all_trackers.update(trackers)
            bars_loaded.update(loaded)
            gate_summaries.update(gates)
            missing_indicators.update(missing)
        if args.mode in {"optimize", "both"}:
            opt_results, recs = run_optimize(
                dsn=args.dsn,
                combos=combos,
                start_date=args.start_date,
                end_date=args.end_date,
                table=args.table,
                max_combos=args.max_combos,
                walk_forward=args.walk_forward,
                strategy_env_path=args.strategy_env,
                execution=execution,
                chunk_size=args.chunk_size,
                train_days=args.train_days,
                test_days=args.test_days,
                step_days=args.step_days,
                parameter_sensitivity=parameter_sensitivity,
                recommendation_payloads=recommendation_payloads,
            )
            all_opt_results.update(opt_results)
            all_recommendations.update(recs)

        summary_path = write_full_report(
            output_dir=args.output_dir,
            all_metrics=all_metrics,
            all_trades=all_trackers,
            optimization_results=all_opt_results,
            recommendations=all_recommendations,
            bars_loaded=bars_loaded,
            gate_summaries=gate_summaries,
            missing_indicators=missing_indicators,
            combo_analyses=combo_analyses,
            parameter_sensitivity=parameter_sensitivity,
            recommendation_payloads=recommendation_payloads,
            strategy_env_path=args.strategy_env,
            execution_summary=execution.as_dict(),
        )
        print(f"\nReport written to: {summary_path}")
        print(f"Trade logs:        {args.output_dir}/trades_*.csv")
        print(f"Raw fills:         {args.output_dir}/fills_*.csv")
        print(f"Gate summaries:    {args.output_dir}/gate_summary_*.csv")
        print(f"Optimization:      {args.output_dir}/optimization_*.csv")
        print(f"Assumptions:       {args.output_dir}/replay_report.md")
        print(f"Recommendations:   {args.output_dir}/recommendations.yaml")
        print(format_invalid_combinations())
        print(format_assumptions(execution.as_dict()))


if __name__ == "__main__":
    main()
