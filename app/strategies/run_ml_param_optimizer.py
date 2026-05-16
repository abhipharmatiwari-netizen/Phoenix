"""
Runner: ML-powered EMA20 parameter optimization.
Finds optimal profitable parameters using Bayesian optimization + lightweight ML models.

Usage:
    python run_ml_param_optimizer.py [--iterations 100] [--output results.json]
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from app.strategies.ml_param_optimizer import (
    Ema20Backtester,
    BayesianOptimizer,
    ParameterSpace,
    ParameterSet,
    ParameterEnsemble,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def get_ema20_parameter_spaces() -> list[ParameterSpace]:
    """Define EMA20 strategy parameter search space."""
    return [
        # Core trend detection
        ParameterSpace(
            name="ema_period",
            param_type="int",
            min_value=10,
            max_value=50,
        ),
        # PR #283 codex round-6 P2: ``signal_timeframe`` was also dropped
        # from the standalone synthetic param space — ``Ema20Backtester.
        # backtest()`` never reads it (runs over its fixed OHLC frame),
        # so a "best timeframe" reported by this script would have been
        # arbitrary noise. The multi-strategy real-data optimizer keeps
        # the knob (its loader honours ``signal_timeframe``).

        # Risk/reward
        ParameterSpace(
            name="sl_pct",
            param_type="float",
            min_value=0.15,
            max_value=0.75,
        ),
        ParameterSpace(
            name="tp_pct",
            param_type="float",
            min_value=0.15,
            max_value=0.75,
        ),

        # Filters
        ParameterSpace(
            name="min_atr",
            param_type="float",
            min_value=0.05,
            max_value=0.50,
        ),
        ParameterSpace(
            name="require_rsi_falling",
            param_type="bool",
        ),
        # PR #283 codex round-4 P2: ``use_adx_filter`` and ``min_adx``
        # were sampled and exported here but ``Ema20Backtester.backtest()``
        # (the standalone synthetic path consumed by this script) never
        # reads them, so the optimizer would report arbitrary "best" ADX
        # values that had zero effect on the score. Either the synthetic
        # backtester learns to honour ADX, or these knobs are removed
        # from the standalone param space; chose the latter since
        # synthetic OHLC has no realistic +DI/-DI / ADX data anyway.
        # The real-data path in ``run_multi_strategy_optimizer.py``
        # exposes ADX-gating via ``Ema20ParameterOptimizer`` and the
        # ``RealDataBacktester._simulate_ema20`` simulator that DOES
        # honour ``use_adx_filter`` / ``min_adx`` (PR #283 round 3).
        #
        # PR #283 codex round-5 P2: ``trail_buffer_pct`` and
        # ``tp1_pct`` were also dropped here for the same reason —
        # ``Ema20Backtester.backtest()`` (the synthetic standalone
        # path) reads only ``ema_period`` / ``sl_pct`` / ``tp_pct`` /
        # ``min_atr`` / ``require_rsi_falling`` (see
        # app/strategies/ml_param_optimizer.py:182-211), so any
        # reported "best" trail-buffer / tp1 value here is noise that
        # could mislead operators copying it into live config.
    ]


def run_optimization(
    n_iterations: int = 100,
    output_file: str = "param_optimization_results.json",
) -> dict:
    """
    Run multi-stage ML parameter optimization.

    Stage 1: Bayesian optimization for rapid exploration
    Stage 2: Genetic algorithm for combination discovery
    Stage 3: Ensemble ranking and Pareto frontier selection
    """
    logger.info("=" * 80)
    logger.info("EMA20 ML-Powered Parameter Optimization")
    logger.info("=" * 80)

    # Stage 0: Setup
    logger.info("\n[Stage 0] Initializing backtester...")
    backtester = Ema20Backtester()
    logger.info(f"  Loaded {len(backtester.ohlc_data)} candles for testing")

    param_spaces = get_ema20_parameter_spaces()
    logger.info(f"  Parameter space: {len(param_spaces)} dimensions")
    for space in param_spaces:
        logger.info(f"    - {space.name} ({space.param_type})")

    # Stage 1: Bayesian Optimization
    logger.info(f"\n[Stage 1] Bayesian Optimization ({int(n_iterations * 0.5)} iterations)...")
    logger.info("  Using Gaussian process + expected improvement heuristic")
    logger.info("  Phase 1a: Random exploration (initial sampling)")
    logger.info("  Phase 1b: Intelligent search (score-guided tuning)")

    optimizer = BayesianOptimizer(param_spaces, backtester)
    best_bayesian = optimizer.optimize(n_iterations=int(n_iterations * 0.5))
    bayesian_sets = optimizer.evaluated.copy()

    logger.info(f"  Bayesian Best Score: {optimizer.best_score:.2f}")
    logger.info(f"  Best Parameters: {best_bayesian}")

    # Stage 2: Genetic Algorithm (mutation/crossover of top performers)
    logger.info(f"\n[Stage 2] Genetic Algorithm ({int(n_iterations * 0.3)} iterations)...")
    logger.info("  Generating parameter combinations via mutation & crossover")

    ga_sets = _genetic_algorithm_stage(
        param_spaces,
        backtester,
        best_performers=sorted(bayesian_sets, key=lambda p: p.score(), reverse=True)[:5],
        n_iterations=int(n_iterations * 0.3),
    )

    # Stage 3: Random Refinement (exploit high-variance regions)
    logger.info(f"\n[Stage 3] Random Refinement ({int(n_iterations * 0.2)} iterations)...")
    logger.info("  Testing corner cases and extreme values")

    refinement_sets = _refinement_stage(
        param_spaces,
        backtester,
        n_iterations=int(n_iterations * 0.2),
    )

    # Combine all results
    all_sets = bayesian_sets + ga_sets + refinement_sets
    logger.info(f"\nTotal configurations evaluated: {len(all_sets)}")

    # PR #283 codex round-4 P2: a CLI invocation with very small
    # ``--iterations`` (e.g. 2 or 3) leaves every stage's per-stage
    # iteration budget rounded to 0, so ``all_sets`` ends up empty and
    # the downstream ``max(p.score() for p in all_sets)`` raised
    # ``ValueError: max() iterable argument is empty``. Fail loudly with
    # an actionable error instead.
    if not all_sets:
        raise SystemExit(
            f"Optimization produced zero configurations across all stages; "
            f"--iterations={n_iterations} is too small. Each stage's "
            f"iteration budget (Bayesian 50%, GA 30%, refinement 20%) "
            f"rounds to zero. Use --iterations >= 10 for any useful run."
        )

    # Stage 4: Ensemble ranking
    logger.info("\n[Stage 4] Ensemble Ranking & Pareto Analysis...")
    ensemble = ParameterEnsemble(all_sets)

    logger.info("\n  Top 10 Configurations (by composite score):")
    for i, param_set in enumerate(all_sets[:10], 1):
        metrics = param_set.metrics
        logger.info(
            f"    {i}. Score={metrics.score:.2f} | "
            f"PnL=${metrics.total_pnl:.0f} | "
            f"WinRate={metrics.win_rate:.1%} | "
            f"Trades={metrics.total_trades}"
        )

    pareto = ensemble.pareto_frontier()
    logger.info(f"\n  Pareto-Optimal Frontier ({len(pareto)} configs):")
    logger.info("    (Configurations that can't be strictly improved on all metrics)")
    for i, params in enumerate(pareto[:5], 1):
        param_set = next(p for p in all_sets if p.params == params)
        metrics = param_set.metrics
        logger.info(
            f"    {i}. EMA={params['ema_period']} | SL={params['sl_pct']:.2%} | "
            f"TP={params['tp_pct']:.2%} | PnL=${metrics.total_pnl:.0f}"
        )

    # Export results
    logger.info("\n[Stage 5] Exporting Results...")
    results = {
        "timestamp": datetime.now().isoformat(),
        "total_iterations": len(all_sets),
        "best_overall_score": max(p.score() for p in all_sets),
        "best_parameters": next(p for p in all_sets if p.score() == max(s.score() for s in all_sets)).params,
        "top_10": [
            {
                "params": p.params,
                "metrics": p.metrics.to_dict(),
            }
            for p in sorted(all_sets, key=lambda x: x.score(), reverse=True)[:10]
        ],
        "pareto_frontier": [
            {
                "params": next(p for p in all_sets if p.params == params).params,
                "metrics": next(p for p in all_sets if p.params == params).metrics.to_dict(),
            }
            for params in pareto
        ],
    }

    output_path = Path(output_file)
    output_path.write_text(json.dumps(results, indent=2, default=str))
    logger.info(f"  Results saved to: {output_path.absolute()}")

    logger.info("\n" + "=" * 80)
    logger.info("Optimization Complete!")
    logger.info("=" * 80)

    return results


def _genetic_algorithm_stage(
    param_spaces: list[ParameterSpace],
    backtester: Ema20Backtester,
    best_performers: list,
    n_iterations: int,
) -> list[ParameterSet]:
    """Genetic algorithm: mutate and crossover top performers."""
    import numpy as np

    results = []

    for iteration in range(n_iterations):
        # Select two random parents from best performers. PR #283 codex
        # round-3 P2: initialise ``parent2`` explicitly so the
        # single-parent branch cannot raise ``UnboundLocalError`` from
        # the ``elif parent2`` crossover check below — previously a
        # ``--iterations`` value small enough to yield a single best
        # performer crashed nondeterministically in the GA stage.
        parent2 = None
        if len(best_performers) >= 2:
            parent1 = best_performers[np.random.randint(len(best_performers))]
            parent2 = best_performers[np.random.randint(len(best_performers))]
        else:
            parent1 = best_performers[0] if best_performers else None

        if not parent1:
            continue

        # Crossover + mutation
        child_params = {}
        for space in param_spaces:
            if parent1 and space.name in parent1.params:
                if np.random.random() < 0.5:
                    # Inherit from parent1
                    val = parent1.params[space.name]
                elif parent2 and space.name in parent2.params:
                    # Inherit from parent2
                    val = parent2.params[space.name]
                else:
                    # Random
                    val = space.sample(np.random.uniform(0, 1))

                # Mutation: small perturbation
                if np.random.random() < 0.1:
                    if space.param_type in ("int", "float"):
                        perturbation = np.random.normal(0, 0.05)
                        if space.param_type == "int":
                            val = int(np.clip(val + perturbation * (space.max_value - space.min_value), space.min_value, space.max_value))
                        else:
                            val = np.clip(val + perturbation * (space.max_value - space.min_value), space.min_value, space.max_value)

                child_params[space.name] = val

        metrics = backtester.backtest(child_params)
        results.append(ParameterSet(params=child_params, metrics=metrics, generation=1))

    return results


def _refinement_stage(
    param_spaces: list[ParameterSpace],
    backtester: Ema20Backtester,
    n_iterations: int,
) -> list[ParameterSet]:
    """Test corner cases and extreme configurations."""
    import numpy as np

    results = []

    for iteration in range(n_iterations):
        params = {}
        for space in param_spaces:
            # 50% chance of extreme value, 50% random
            if np.random.random() < 0.5 and space.param_type not in ("categorical", "bool"):
                # Extreme: min or max
                val = space.min_value if np.random.random() < 0.5 else space.max_value
                if space.min_value and space.max_value:
                    normalized = (val - space.min_value) / (space.max_value - space.min_value)
                    params[space.name] = space.sample(normalized)
                else:
                    params[space.name] = space.sample(np.random.uniform(0, 1))
            else:
                # Random
                params[space.name] = space.sample(np.random.uniform(0, 1))

        metrics = backtester.backtest(params)
        results.append(ParameterSet(params=params, metrics=metrics, generation=2))

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ML-powered EMA20 parameter optimization"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="Total optimization iterations (default: 100)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="param_optimization_results.json",
        help="Output file for results (default: param_optimization_results.json)",
    )

    args = parser.parse_args()

    results = run_optimization(
        n_iterations=args.iterations,
        output_file=args.output,
    )
