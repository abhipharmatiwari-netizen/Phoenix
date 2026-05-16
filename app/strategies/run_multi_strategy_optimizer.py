"""
Master optimizer: Validates framework on EMA20, Exclusive Nifty CE, and Put Momentum
using real PostgreSQL indicator data for NIFTY, BANKNIFTY, NATURALGAS.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from app.strategies.ml_param_optimizer import (
    ParameterSet,
    ParameterEnsemble,
    BacktestMetrics,
    _compute_profit_factor,
)
from app.strategies.postgres_data_loader import (
    PostgresIndicatorLoader,
    RealDataBacktester,
)
from app.strategies.strategy_optimizers import StrategyOptimizationRunner

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


class MultiStrategyOptimizer:
    """Orchestrates optimization across all three strategies and underlyings."""

    def __init__(self, postgres_dsn: Optional[str] = None):
        self.postgres_dsn = postgres_dsn
        self.loader = PostgresIndicatorLoader(dsn=postgres_dsn)
        self.backtester = RealDataBacktester(self.loader)
        self.runner = StrategyOptimizationRunner()
        self.results = {}

    def optimize_strategy(
        self,
        strategy_name: str,
        underlying_label: str,
        n_iterations: int = 100,
    ) -> Dict[str, Any]:
        """Optimize a single strategy on a single underlying."""
        logger.info(f"\n{'='*80}")
        logger.info(f"Optimizing {strategy_name.upper()} on {underlying_label}")
        logger.info(f"{'='*80}")

        # Get parameter spaces
        param_spaces = self.runner.get_parameter_spaces(strategy_name)
        logger.info(f"Parameter space: {len(param_spaces)} dimensions")
        for space in param_spaces:
            logger.info(f"  - {space.name} ({space.param_type})")

        # Create custom backtester for this strategy/underlying
        def backtest_func(params: Dict[str, Any]) -> BacktestMetrics:
            """Backtest wrapper that uses real data."""
            formatted = self.runner.format_parameters(strategy_name, params)

            if strategy_name == "ema20":
                result = self.backtester.backtest_ema20(formatted, underlying_label)
            elif strategy_name == "exclusive_nifty_ce":
                result = self.backtester.backtest_exclusive_nifty_ce(formatted, underlying_label)
            elif strategy_name == "put_momentum":
                result = self.backtester.backtest_put_momentum(formatted, underlying_label)
            else:
                result = {}

            # Convert to BacktestMetrics. PR #283 codex round-3 P2: also
            # populate winning_trades / losing_trades / profit_factor so
            # the composite score's ``win_rate * profit_factor`` consistency
            # term is not zeroed for every real-data run. The simulators
            # return ``gross_win`` and ``gross_loss`` exactly so we can
            # reuse ``_compute_profit_factor`` without re-deriving the
            # per-trade pnls here.
            winning_trades = int(result.get("winning_trades", 0))
            losing_trades = int(result.get("losing_trades", 0))
            gross_win = float(result.get("gross_win", 0.0))
            gross_loss = float(result.get("gross_loss", 0.0))
            profit_factor = _compute_profit_factor(
                # ``_compute_profit_factor`` only looks at sum/len of each
                # list, so we can hand it singletons that carry the same
                # sums; this avoids reconstructing the full pnl arrays.
                [gross_win] if winning_trades else [],
                [gross_loss] if losing_trades else [],
            )
            metrics = BacktestMetrics(
                total_trades=int(result.get("total_trades", 0)),
                winning_trades=winning_trades,
                losing_trades=losing_trades,
                total_pnl=float(result.get("total_pnl", 0)),
                win_rate=float(result.get("win_rate", 0)),
                sharpe_ratio=float(result.get("sharpe_ratio", 0)),
                max_drawdown=float(result.get("max_drawdown", 0)),
                profit_factor=profit_factor,
            )
            return metrics

        # Run optimization
        logger.info(f"\n[Stage 1] Bayesian Optimization ({int(n_iterations * 0.5)} iterations)...")

        evaluated = []
        best_score = -float('inf')
        best_params = None

        # Random exploration
        logger.info("  Phase 1a: Random exploration...")
        import numpy as np
        np.random.seed(42)

        for i in range(int(n_iterations * 0.5)):
            params = {}
            for space in param_spaces:
                rand_val = np.random.uniform(0, 1)
                params[space.name] = space.sample(rand_val)

            metrics = backtest_func(params)
            param_set = ParameterSet(params=params, metrics=metrics)
            evaluated.append(param_set)

            if metrics.score > best_score:
                best_score = metrics.score
                best_params = params

            if (i + 1) % 10 == 0:
                logger.info(f"    Iteration {i + 1}: Best score = {best_score:.2f}")

        logger.info(f"\n[Stage 2] Genetic Algorithm ({int(n_iterations * 0.3)} iterations)...")
        for i in range(int(n_iterations * 0.3)):
            # Mutate best performer
            child_params = {}
            for space in param_spaces:
                if best_params and space.name in best_params:
                    val = best_params[space.name]
                    if space.param_type in ("int", "float"):
                        perturbation = np.random.normal(0, 0.05)
                        if space.param_type == "int":
                            val = int(np.clip(
                                val + perturbation * (space.max_value - space.min_value),
                                space.min_value,
                                space.max_value
                            ))
                        else:
                            val = np.clip(
                                val + perturbation * (space.max_value - space.min_value),
                                space.min_value,
                                space.max_value
                            )
                    child_params[space.name] = val
                else:
                    child_params[space.name] = space.sample(np.random.uniform(0, 1))

            metrics = backtest_func(child_params)
            param_set = ParameterSet(params=child_params, metrics=metrics)
            evaluated.append(param_set)

            if metrics.score > best_score:
                best_score = metrics.score
                best_params = child_params

        logger.info("\n[Stage 3] Ensemble Analysis...")
        ensemble = ParameterEnsemble(evaluated)
        pareto = ensemble.pareto_frontier()

        logger.info(f"  Total configurations tested: {len(evaluated)}")
        logger.info(f"  Best score: {best_score:.2f}")
        logger.info("  Top 5 configurations:")

        for i, param_set in enumerate(evaluated[:5], 1):
            m = param_set.metrics
            logger.info(
                f"    {i}. Score={m.score:.2f} | PnL=${m.total_pnl:.0f} | "
                f"WinRate={m.win_rate:.1%} | Trades={m.total_trades}"
            )

        # Compile results
        results = {
            "strategy": strategy_name,
            "underlying": underlying_label,
            "timestamp": datetime.now().isoformat(),
            "total_iterations": len(evaluated),
            "best_score": best_score,
            "best_parameters": best_params,
            "top_5": [
                {
                    "params": p.params,
                    "metrics": p.metrics.to_dict(),
                }
                for p in sorted(evaluated, key=lambda x: x.score(), reverse=True)[:5]
            ],
            "pareto_frontier": [
                {
                    "params": next(p for p in evaluated if p.params == params).params,
                    "metrics": next(p for p in evaluated if p.params == params).metrics.to_dict(),
                }
                for params in pareto
            ],
        }

        return results

    def optimize_all(
        self,
        n_iterations: int = 100,
        strategies: Optional[List[str]] = None,
        underlyings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Optimize all strategies on all underlyings."""
        logger.info("\n" + "="*80)
        logger.info("MULTI-STRATEGY OPTIMIZATION ON POSTGRESQL DATA")
        logger.info("="*80)

        strategies = strategies or list(self.runner.get_strategies().keys())
        self.results = {}

        for strategy in strategies:
            strategy_underlyings = underlyings or self.runner.get_underlyings_for_strategy(strategy)

            self.results[strategy] = {}

            for underlying in strategy_underlyings:
                try:
                    result = self.optimize_strategy(strategy, underlying, n_iterations)
                    self.results[strategy][underlying] = result
                except Exception as e:
                    logger.error(f"Error optimizing {strategy} on {underlying}: {e}", exc_info=True)
                    self.results[strategy][underlying] = {"error": str(e)}

        return self.results

    def export_results(self, output_file: str = "multi_strategy_optimization_results.json"):
        """Export optimization results to JSON."""
        output_path = Path(output_file)
        output_path.write_text(json.dumps(self.results, indent=2, default=str))
        logger.info(f"\nResults exported to: {output_path.absolute()}")

    def generate_comparison_report(self) -> str:
        """Generate comparison report across strategies and underlyings."""
        report = []
        report.append("\n" + "="*80)
        report.append("CROSS-STRATEGY COMPARISON REPORT")
        report.append("="*80)

        # Summary table
        report.append("\n## Performance Summary\n")
        report.append("| Strategy | Underlying | Best Score | PnL | Win Rate | Trades |")
        report.append("|----------|-----------|-----------|-----|----------|--------|")

        for strategy, underlyings_dict in self.results.items():
            for underlying, result in underlyings_dict.items():
                if "error" in result:
                    continue

                if result.get("top_5"):
                    top_metrics = result["top_5"][0]["metrics"]
                    report.append(
                        f"| {strategy:15} | {underlying:12} | {top_metrics['score']:9.2f} | "
                        f"${top_metrics['total_pnl']:7.0f} | {top_metrics['win_rate']:7.1%} | "
                        f"{top_metrics['total_trades']:6} |"
                    )

        # Strategy recommendations
        report.append("\n## Recommendations\n")
        for strategy, underlyings_dict in self.results.items():
            report.append(f"\n### {strategy.upper()}\n")

            best_overall = None
            best_score = -float('inf')

            for underlying, result in underlyings_dict.items():
                if "error" in result:
                    continue
                if result.get("best_score", -float('inf')) > best_score:
                    best_score = result["best_score"]
                    best_overall = (underlying, result)

            if best_overall:
                underlying, result = best_overall
                best_params = result.get("best_parameters", {})
                top_metrics = result["top_5"][0]["metrics"] if result.get("top_5") else {}

                report.append(f"**Best Performance:** {underlying}")
                report.append(f"- Score: {top_metrics.get('score', 0):.2f}")
                report.append(f"- Total PnL: ${top_metrics.get('total_pnl', 0):.0f}")
                report.append(f"- Win Rate: {top_metrics.get('win_rate', 0):.1%}")
                report.append(f"- Max Drawdown: ${top_metrics.get('max_drawdown', 0):.0f}")
                report.append("\n**Optimal Parameters:**")

                for key, value in sorted(best_params.items()):
                    if isinstance(value, float):
                        report.append(f"- {key}: {value:.4f}")
                    else:
                        report.append(f"- {key}: {value}")

        return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(
        description="Multi-strategy optimization on PostgreSQL data"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="Total iterations per strategy/underlying (default: 100)"
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=["ema20", "exclusive_nifty_ce", "put_momentum"],
        help="Strategies to optimize (default: all)"
    )
    parser.add_argument(
        "--underlyings",
        nargs="+",
        # PR #283 codex round-2 P1: match indicator_bars.label values
        # (``*_IDX`` for NSE indexes, ``NG_FUT`` for natural-gas future).
        choices=["NIFTY_IDX", "BANKNIFTY_IDX", "NG_FUT"],
        help="Underlyings to optimize (default: all)"
    )
    parser.add_argument(
        "--dsn",
        type=str,
        help="PostgreSQL DSN (default: from environment)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="multi_strategy_optimization_results.json",
        help="Output JSON file"
    )

    args = parser.parse_args()

    try:
        optimizer = MultiStrategyOptimizer(postgres_dsn=args.dsn)
        optimizer.optimize_all(
            n_iterations=args.iterations,
            strategies=args.strategies,
            underlyings=args.underlyings,
        )
        optimizer.export_results(args.output)

        report = optimizer.generate_comparison_report()
        print(report)

        # Also export report
        report_path = Path(args.output).with_suffix(".md")
        report_path.write_text(report)
        logger.info(f"Report exported to: {report_path.absolute()}")

    except Exception as e:
        logger.error(f"Optimization failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
