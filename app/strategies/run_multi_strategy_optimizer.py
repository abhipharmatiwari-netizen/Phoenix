"""
Master optimizer: Validates framework on EMA20, Exclusive Nifty CE, and Put Momentum
using real PostgreSQL indicator data for NIFTY, BANKNIFTY, NATURALGAS.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.strategies.candidate_writer import (
    CandidateBatch,
    CandidateWriter,
    CandidateWriterError,
)
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
from app.strategies.walk_forward_validator import (
    WalkForwardConfig,
    WalkForwardValidator,
)


# (strategy_name, params) → simulator call. Used by the walk-forward
# gate and any other code that needs to score a candidate on an
# arbitrary df slice. Keeps the mapping in one place so the gate stays
# in sync with the live ``MultiStrategyOptimizer.backtest_func`` dispatch.
_STRATEGY_TO_SIMULATOR = {
    "ema20": RealDataBacktester._simulate_ema20,
    "exclusive_nifty_ce": RealDataBacktester._simulate_exclusive_nifty_ce,
    "put_momentum": RealDataBacktester._simulate_put_momentum,
}


# Timeframe used by each strategy when fetching ``indicator_bars`` for
# the walk-forward gate. Mirrors ``RealDataBacktester.backtest_*``.
_STRATEGY_TIMEFRAMES = {
    "ema20": 300,
    "exclusive_nifty_ce": 30,
    "put_momentum": 300,
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def _env_int_default(env_name: str, fallback: int) -> int:
    """Read an env-var as int with safe fallback.

    PR #288 codex round-5 P3: argparse evaluates ``default=`` at parser
    construction time, so a bad ambient ``OPTIMIZER_*`` env-var
    (``"invalid"``, ``""``, etc.) would crash via ``int(...)`` before
    argparse could process a valid CLI override. Wrap the conversion so
    a broken env-var is logged once and falls back to the documented
    default — the operator can still pass the right value on the CLI.
    """
    raw = os.getenv(env_name, "")
    if raw == "":
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r; using fallback %d",
            env_name,
            raw,
            fallback,
        )
        return fallback


def _env_float_default(env_name: str, fallback: float) -> float:
    """Read an env-var as float with safe fallback (see _env_int_default)."""
    raw = os.getenv(env_name, "")
    if raw == "":
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r; using fallback %.4f",
            env_name,
            raw,
            fallback,
        )
        return fallback


class MultiStrategyOptimizer:
    """Orchestrates optimization across all three strategies and underlyings."""

    def __init__(
        self,
        postgres_dsn: Optional[str] = None,
        *,
        lookback_days: int = 20,
        loader_end_date: Optional[date] = None,
    ) -> None:
        """
        Args:
            postgres_dsn: Connection string for indicator_bars.
            lookback_days: Backtest window length in days. Threaded through
                to ``RealDataBacktester`` so the candidate writer's
                ``backtest_window`` field reflects the data the simulator
                actually scored on (PR #288 codex round-1 P2). Previously
                the backtester hardcoded ``days_back=20`` while the
                writer recorded whatever the CLI specified, making
                promoted candidates non-reproducible when
                ``--lookback-days != 20``.
            loader_end_date: PR #288 codex round-5 P2 — captured IST
                date used as ``end_date`` for every loader call. Without
                this, ``fetch_indicator_bars`` re-evaluates
                ``datetime.now(IST).date()`` per query, so a run
                spanning IST midnight queries different windows for
                different candidates while the writer records a single
                ``backtest_window`` based on the start-of-run date.
        """
        self.postgres_dsn = postgres_dsn
        self.lookback_days = max(1, int(lookback_days))
        self.loader_end_date = loader_end_date
        self.loader = PostgresIndicatorLoader(dsn=postgres_dsn)
        self.backtester = RealDataBacktester(
            self.loader,
            lookback_days=self.lookback_days,
            end_date=loader_end_date,
        )
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
            # PR #289 codex round-3 P2: also export the full ranked
            # candidate list so the walk-forward gate can validate
            # beyond the legacy ``top_5`` when many of the top-ranked
            # ones fail. Without this, ``--candidates-per-strategy=3``
            # could leave the review queue short even when valid
            # candidates exist further down the ranking.
            "ranked_candidates": [
                {
                    "params": p.params,
                    "metrics": p.metrics.to_dict(),
                }
                for p in sorted(evaluated, key=lambda x: x.score(), reverse=True)
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

        # PR #283 codex round-5 P2: ``--iterations`` low enough to round
        # every stage budget (``int(n * 0.5)`` for Bayesian,
        # ``int(n * 0.3)`` for GA) to zero produces an empty
        # ``evaluated`` list and a result dict with ``best_score=-inf``,
        # ``best_parameters=None``, and an empty ``top_5`` — JSON
        # serialization then emits the non-standard ``-Infinity`` token.
        # Fail loudly the same way the standalone runner does
        # (``run_ml_param_optimizer.run_optimization``) so an operator
        # sees the actionable message instead of unusable JSON.
        if int(n_iterations * 0.5) < 1:
            raise SystemExit(
                f"--iterations={n_iterations} is too small for the "
                f"multi-strategy optimizer: each stage's budget "
                f"(Bayesian 50%%, GA 30%%) rounds to zero. Use "
                f"--iterations >= 10 for any useful run."
            )

        strategies = strategies or list(self.runner.get_strategies().keys())
        self.results = {}

        for strategy in strategies:
            allowed_for_strategy = self.runner.get_underlyings_for_strategy(strategy)
            if underlyings:
                # PR #283 codex round-5 P2: intersect the user's
                # ``--underlyings`` request with each strategy's
                # allowlist. Without this, e.g.
                # ``--strategies exclusive_nifty_ce --underlyings NG_FUT``
                # would score the NIFTY-only CE strategy on natgas bars
                # (if any exist), producing recommendations for a
                # strategy/instrument combination the runner declares
                # unsupported.
                strategy_underlyings = [u for u in underlyings if u in allowed_for_strategy]
                if not strategy_underlyings:
                    logger.warning(
                        "No requested --underlyings are supported for strategy=%s "
                        "(supported: %s; requested: %s) — skipping.",
                        strategy,
                        allowed_for_strategy,
                        list(underlyings),
                    )
            else:
                strategy_underlyings = allowed_for_strategy

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
    # Issue #272: optional Postgres promotion of top-N candidates into
    # public.strategy_config_candidates (status='pending'). The live
    # strategy_configs row is never mutated by this flag — that's the
    # admin approval API's job (#275).
    parser.add_argument(
        "--promote-to-candidate",
        action="store_true",
        help=(
            "After optimization, persist top-N candidates per (strategy, "
            "underlying) into the strategy_config_candidates review queue. "
            "Existing pending rows with identical params are superseded."
        ),
    )
    parser.add_argument(
        "--candidates-per-strategy",
        type=int,
        default=3,
        help=(
            "When --promote-to-candidate is set, the maximum number of "
            "top candidates per (strategy, underlying) to insert as "
            "'pending' (default: 3)."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=os.getenv("OPTIMIZER_TENANT_ID", ""),
        help=(
            "Tenant scoping for strategy_configs lookup (default: env "
            "OPTIMIZER_TENANT_ID). Required when --promote-to-candidate."
        ),
    )
    parser.add_argument(
        "--broker-account-id",
        type=str,
        default=os.getenv("OPTIMIZER_BROKER_ACCOUNT_ID", ""),
        help=(
            "Broker account scoping for strategy_configs lookup (default: "
            "env OPTIMIZER_BROKER_ACCOUNT_ID). Required when "
            "--promote-to-candidate."
        ),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=_env_int_default("OPTIMIZER_LOOKBACK_DAYS", 20),
        help=(
            "Backtest lookback in days; used as the recorded "
            "backtest_window when --promote-to-candidate is set "
            "(default: env OPTIMIZER_LOOKBACK_DAYS or 20)."
        ),
    )
    # PR #288 codex round-5 P2: split-DB support. When ``--dsn`` points
    # at a separate indicator-bars DB (which doesn't have
    # ``strategy_configs`` / ``strategy_config_candidates``), the
    # candidate writer needs a different connection string. Default is
    # to share ``--dsn`` so the typical single-DB setup keeps working.
    parser.add_argument(
        "--candidate-writer-dsn",
        type=str,
        default=os.getenv("OPTIMIZER_CANDIDATE_WRITER_DSN", ""),
        help=(
            "Postgres DSN for the candidate writer (defaults to --dsn). "
            "Override when indicator_bars and strategy_configs live in "
            "different databases (env: OPTIMIZER_CANDIDATE_WRITER_DSN)."
        ),
    )
    # Issue #273: walk-forward + OOS gate before candidate insert.
    parser.add_argument(
        "--disable-walk-forward-gate",
        action="store_true",
        default=os.getenv("OPTIMIZER_DISABLE_WALK_FORWARD_GATE", "").lower()
        in ("1", "true", "yes"),
        help=(
            "Bypass the walk-forward + OOS holdout filter and insert every "
            "top-N candidate into the review queue regardless of stability. "
            "Off by default -- the gate is ALWAYS ON for --promote-to-candidate "
            "in production runs so unstable parameters cannot surface for "
            "approval without an explicit override."
        ),
    )
    parser.add_argument(
        "--walk-forward-folds",
        type=int,
        default=_env_int_default("OPTIMIZER_WALK_FORWARD_FOLDS", 4),
        help="Number of in-sample folds for the walk-forward gate (default: 4).",
    )
    parser.add_argument(
        "--oos-holdout-pct",
        type=float,
        default=_env_float_default("OPTIMIZER_OOS_HOLDOUT_PCT", 0.20),
        help=(
            "Fraction of the trailing data reserved as the out-of-sample "
            "holdout for the walk-forward gate (default: 0.20)."
        ),
    )
    parser.add_argument(
        "--min-trades-per-fold",
        type=int,
        default=_env_int_default("OPTIMIZER_MIN_TRADES_PER_FOLD", 5),
        help=(
            "Minimum trade count per fold for a candidate to pass the gate. "
            "Folds with fewer than this many trades are not statistically "
            "meaningful (default: 5)."
        ),
    )
    parser.add_argument(
        "--max-in-sample-degradation-pct",
        type=float,
        default=_env_float_default("OPTIMIZER_MAX_DEGRADATION_PCT", 0.30),
        help=(
            "Maximum allowed drop from in-sample score to median fold score "
            "before the gate rejects (default: 0.30; folds must score "
            "within 70%% of in-sample)."
        ),
    )

    args = parser.parse_args()

    try:
        # PR #288 codex round-4 P2: capture the IST date BEFORE any
        # loader call so the candidate writer's ``backtest_window``
        # matches the actual end-date the loader saw, even if the run
        # spans IST midnight.
        from app.strategies.postgres_data_loader import IST as _IST
        loader_end_date = datetime.now(_IST).date()

        optimizer = MultiStrategyOptimizer(
            postgres_dsn=args.dsn,
            lookback_days=args.lookback_days,
            # PR #288 codex round-5 P2: thread the captured date so
            # every ``fetch_indicator_bars`` call uses the SAME end
            # date instead of re-evaluating ``datetime.now(IST).date()``
            # per query — eliminates the IST-midnight drift between
            # the data the simulator scored on and the
            # ``backtest_window`` recorded with the candidate.
            loader_end_date=loader_end_date,
        )
        results = optimizer.optimize_all(
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

        if args.promote_to_candidate:
            # PR #288 codex round-5 P2: split-DB support -- when set,
            # ``--candidate-writer-dsn`` overrides ``--dsn`` for the
            # writer's ``strategy_configs`` / ``strategy_config_candidates``
            # connection. Defaults to ``--dsn`` so the typical single-DB
            # case keeps working.
            writer_dsn = args.candidate_writer_dsn or args.dsn

            # Issue #273: walk-forward + OOS gate.
            walk_forward_config: Optional[WalkForwardConfig]
            if args.disable_walk_forward_gate:
                walk_forward_config = None
                logger.warning(
                    "Walk-forward gate DISABLED via --disable-walk-forward-gate; "
                    "every top-N candidate will reach the review queue without "
                    "stability validation."
                )
            else:
                walk_forward_config = WalkForwardConfig(
                    folds=args.walk_forward_folds,
                    oos_holdout_pct=args.oos_holdout_pct,
                    min_trades_per_fold=args.min_trades_per_fold,
                    max_in_sample_degradation_pct=args.max_in_sample_degradation_pct,
                )
            _promote_top_candidates(
                results=results,
                tenant_id=args.tenant_id,
                broker_account_id=args.broker_account_id,
                lookback_days=args.lookback_days,
                candidates_per_strategy=args.candidates_per_strategy,
                dsn=writer_dsn,
                # PR #289 codex round-2 P2: pass the INDICATOR DSN
                # separately so the walk-forward gate's
                # ``PostgresIndicatorLoader`` queries the indicator DB
                # even in a split-DB setup where ``writer_dsn`` points
                # at the control-plane DB.
                indicator_dsn=args.dsn,
                loader_end_date=loader_end_date,
                walk_forward_config=walk_forward_config,
            )

    except Exception as e:
        logger.error(f"Optimization failed: {e}", exc_info=True)
        raise


def _promote_top_candidates(
    *,
    results: Dict[str, Any],
    tenant_id: str,
    broker_account_id: str,
    lookback_days: int,
    candidates_per_strategy: int,
    dsn: Optional[str],
    indicator_dsn: Optional[str] = None,
    loader_end_date: Optional[date] = None,
    walk_forward_config: Optional[WalkForwardConfig] = None,
) -> None:
    """Insert top-N candidates per (strategy, underlying) into the review queue.

    Per-strategy ``CandidateWriterError`` is logged and skipped — one
    missing ``strategy_configs`` row must not abort the rest of the
    run. Any OTHER exception (loader unreachable, missing
    ``strategy_config_candidates`` table, generic SQL error) propagates
    out so an infrastructure failure surfaces as a non-zero exit and a
    failed CI / cron run — PR #288 codex round-4 P2. The previous
    catch-all ``except Exception`` made a broken control-plane DB look
    like a successful "0 rows inserted" nightly.

    Each candidate insert runs in its own transaction
    (see ``CandidateWriter``).

    ``loader_end_date`` (PR #288 codex round-4 P2): when supplied, the
    ``backtest_window`` is anchored on this date instead of
    ``datetime.now(IST).date()``. Callers that already invoked the
    loader earlier in the run pass the same date the loader used; this
    keeps the recorded ``backtest_window`` aligned with the data even
    when the run spans IST midnight.

    Issue #273: when ``walk_forward_config`` is provided, each top-N
    candidate is replayed via the same simulator against K in-sample
    folds + an OOS holdout slice of the loaded indicator bars. Only
    candidates that pass the stability checks are handed to the writer;
    failing candidates are dropped with a structured log line and never
    reach the review queue. The augmented walk-forward metrics
    (``fold_scores``, ``oos_holdout_score``, etc.) are merged into each
    surviving candidate's ``metrics`` JSONB so reviewers can see why
    the gate accepted it.
    """
    if not tenant_id or not broker_account_id:
        raise SystemExit(
            "--promote-to-candidate requires --tenant-id and "
            "--broker-account-id (or OPTIMIZER_TENANT_ID / "
            "OPTIMIZER_BROKER_ACCOUNT_ID env vars)."
        )
    # PR #288 codex round-3 P2: derive ``today`` from the same IST clock
    # the loader uses for its query end date
    # (``datetime.now(IST).date()`` in
    # ``PostgresIndicatorLoader.fetch_indicator_bars``). The previous
    # ``date.today()`` returned the container's local date (UTC in
    # production), so a nightly run between 18:30–23:59 UTC stored a
    # ``backtest_window`` one IST day earlier than the data actually
    # loaded — making promoted candidates non-reproducible in that
    # exact window.
    #
    # PR #288 codex round-4 P2: when ``loader_end_date`` is supplied,
    # use it directly. The loader uses ``datetime.now(IST).date()`` at
    # CALL time, so a backtest that started before IST midnight and a
    # promotion that runs after midnight saw a different "today". If
    # the caller wants reproducibility they should capture the date at
    # backtest-start time and pass it here.
    from app.strategies.postgres_data_loader import IST as _IST
    today = loader_end_date if loader_end_date is not None else datetime.now(_IST).date()
    window = (today - timedelta(days=max(1, lookback_days)), today)
    writer = CandidateWriter(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        dsn=dsn,
    )
    validator = (
        WalkForwardValidator(walk_forward_config)
        if walk_forward_config is not None
        else None
    )
    # Loader for the gate. We share one instance across all (strategy,
    # underlying) pairs so the connection is reused. PR #289 codex
    # round-3 P2: pass the EXPLICIT ``indicator_dsn`` only — DO NOT
    # fall back to ``dsn`` (writer / control-plane). In a split-DB
    # deployment the writer DSN points at a database that has
    # ``strategy_configs`` but no ``indicator_bars``, so falling back
    # would either fail the gate or silently query the wrong source.
    # When ``indicator_dsn`` is ``None``, the loader honours its own
    # default discovery (``PG_INDICATORS_DSN`` env / settings), which
    # is the same source the optimizer's loader uses by default —
    # keeping the gate's validation frame consistent with the
    # optimizer's scoring frame.
    gate_loader = (
        PostgresIndicatorLoader(dsn=indicator_dsn)
        if validator is not None
        else None
    )
    logger.info(
        "Promoting top-%d candidates per (strategy, underlying) "
        "into strategy_config_candidates (optimizer_version=%s, window=%s..%s, "
        "walk_forward=%s)",
        candidates_per_strategy,
        writer.optimizer_version,
        window[0],
        window[1],
        "ON" if validator else "OFF",
    )
    total_inserted = 0
    total_dropped_by_gate = 0
    for strategy_name, by_underlying in (results or {}).items():
        for underlying_label, result in (by_underlying or {}).items():
            if not isinstance(result, dict) or "error" in result:
                logger.warning(
                    "Skipping %s/%s — no usable result (%s)",
                    strategy_name,
                    underlying_label,
                    result.get("error") if isinstance(result, dict) else type(result).__name__,
                )
                continue
            top = result.get("top_5") or []
            if not top:
                logger.warning(
                    "Skipping %s/%s — empty top_5", strategy_name, underlying_label
                )
                continue
            # PR #289 codex round-3 P2: the gate validates the FULL
            # ranked list when available so it can backfill survivors
            # past position 5 if the leaders fail. ``top_5`` remains
            # the writer's input contract (final survivors are sliced
            # to ``candidates_per_strategy``).
            ranked = result.get("ranked_candidates") or top

            # Issue #273 Stage 5: walk-forward gate. Drop unstable
            # candidates BEFORE handing them to the writer.
            if validator is not None and gate_loader is not None:
                gated_top, dropped_count = _apply_walk_forward_gate(
                    strategy_name=strategy_name,
                    underlying_label=underlying_label,
                    candidates=list(ranked),
                    candidates_per_strategy=candidates_per_strategy,
                    lookback_days=lookback_days,
                    validator=validator,
                    loader=gate_loader,
                    # PR #289 codex round-1 P2: thread the captured
                    # ``loader_end_date`` into the gate's fetch so the
                    # walk-forward validation queries the same window
                    # ``top_5`` was originally scored on — even across
                    # IST midnight.
                    loader_end_date=loader_end_date,
                )
                total_dropped_by_gate += dropped_count
                if not gated_top:
                    logger.warning(
                        "Skipping %s/%s — every candidate failed the "
                        "walk-forward gate (%d dropped).",
                        strategy_name,
                        underlying_label,
                        dropped_count,
                    )
                    continue
                top = gated_top

            try:
                batch = CandidateBatch(
                    strategy_name=strategy_name,
                    underlying_label=underlying_label,
                    top_candidates=top,
                    backtest_window=window,
                )
                inserted = writer.write_batch(
                    batch, candidates_per_strategy=candidates_per_strategy
                )
                total_inserted += len(inserted)
            except CandidateWriterError as exc:
                # Expected per-(strategy, underlying) misconfig — log and
                # continue. Missing strategy_configs row, disabled
                # registry rows, malformed candidate.params, etc.
                logger.error(
                    "Candidate write failed for %s/%s: %s",
                    strategy_name,
                    underlying_label,
                    exc,
                )
            # NOTE PR #288 codex round-4 P2: any OTHER exception
            # (loader unreachable, missing candidates table, generic
            # SQL error, broken connection mid-batch) propagates out.
            # An infrastructure failure must not be silently absorbed
            # into a "0 rows inserted" success log.
    if gate_loader is not None:
        try:
            gate_loader.disconnect()
        except Exception:
            logger.debug("gate_loader disconnect raised", exc_info=True)
    logger.info(
        "Candidate promotion complete: %d rows inserted, %d dropped by "
        "walk-forward gate, across all (strategy, underlying) pairs.",
        total_inserted,
        total_dropped_by_gate,
    )


def _apply_walk_forward_gate(
    *,
    strategy_name: str,
    underlying_label: str,
    candidates: List[Dict[str, Any]],
    candidates_per_strategy: int,
    lookback_days: int,
    validator: WalkForwardValidator,
    loader: PostgresIndicatorLoader,
    loader_end_date: Optional[date] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Replay each candidate via the walk-forward validator and return
    the survivors + the count dropped. Survivors carry the augmented
    walk-forward metrics in ``candidate["metrics"]``.

    Loader fetch happens at most once per (strategy, underlying) so the
    K+1 simulator calls per candidate share the same dataframe.

    PR #289 codex round-1 P2: ``loader_end_date`` is the IST date the
    optimizer captured at run start. Passing it here ensures the gate's
    validation frame is anchored on the SAME window the optimizer's
    ``top_5`` was scored on — across IST midnight the loader's default
    ``datetime.now(IST).date()`` would otherwise drift forward a day
    and the gate would validate/reject candidates against the next
    day's window.
    """
    score_fn = _STRATEGY_TO_SIMULATOR.get(strategy_name)
    if score_fn is None:
        logger.warning(
            "walk_forward: no simulator wired for strategy=%s; "
            "bypassing gate for %s/%s",
            strategy_name,
            strategy_name,
            underlying_label,
        )
        return candidates[:candidates_per_strategy], 0
    # PR #289 codex round-2 P2: per-candidate timeframe lookup so a
    # candidate optimized on a non-default ``signal_timeframe`` /
    # ``timeframe_seconds`` is replayed against the SAME bar stream
    # that produced its ``top_5`` score. Cache fetches by timeframe so
    # candidates with identical timeframes share one query.
    default_timeframe = _STRATEGY_TIMEFRAMES.get(strategy_name, 300)
    df_cache: Dict[int, Any] = {}

    def _fetch_for_timeframe(timeframe: int):
        if timeframe in df_cache:
            return df_cache[timeframe]
        # PR #289 codex round-2 P2: fail-CLOSED on loader exceptions.
        # The previous handler logged and bypassed the gate, which let
        # a transient DB blip silently promote unvalidated parameters.
        # Now the exception propagates so the outer
        # ``_promote_top_candidates`` (which catches ``CandidateWriterError``
        # but not arbitrary exceptions per round-4 P2) aborts the run.
        df_local = loader.fetch_indicator_bars(
            underlying_label=underlying_label,
            timeframe_seconds=timeframe,
            days_back=lookback_days,
            end_date=loader_end_date,
        )
        df_cache[timeframe] = df_local
        return df_local

    # PR #289 codex round-3 P2: previously a default-timeframe probe
    # bypassed the gate when only the default stream was empty. That
    # silently promoted unvalidated candidates whose optimized
    # timeframe (e.g. EMA20 60s / 600s, ECN 60s) had data even though
    # 300s did not. The probe is removed; the per-candidate loop below
    # already handles the per-timeframe empty-df case by dropping that
    # candidate with a warning rather than bypassing the whole gate.

    survivors: List[Dict[str, Any]] = []
    dropped = 0
    # PR #289 codex round-2 P3: validate ALL ranked candidates if we
    # don't yet have ``candidates_per_strategy`` survivors. Previously
    # the slice ``[: 2 * candidates_per_strategy]`` could exclude valid
    # later candidates when the first ones failed the gate.
    for candidate in candidates:
        if len(survivors) >= candidates_per_strategy:
            break
        params = candidate.get("params") or {}
        # PR #289 codex round-2 P2: per-candidate timeframe.
        candidate_tf = int(
            params.get(
                "signal_timeframe" if strategy_name == "ema20" else "timeframe_seconds",
                default_timeframe,
            )
        )
        df = _fetch_for_timeframe(candidate_tf)
        if df is None or df.empty:
            logger.warning(
                "walk_forward: empty indicator_bars for %s/%s @ %ds — "
                "skipping candidate (no data to validate against)",
                strategy_name,
                underlying_label,
                candidate_tf,
            )
            dropped += 1
            continue
        result = validator.validate(df, params, score_fn)
        if not result.passed:
            logger.info(
                "walk_forward: dropped %s/%s candidate — reasons=%s "
                "(in_sample=%.2f, median_fold=%.2f, oos=%.2f, min_fold_trades=%d)",
                strategy_name,
                underlying_label,
                result.failure_reasons,
                result.in_sample_score,
                result.median_fold_score,
                result.oos_holdout_score,
                result.min_fold_trades,
            )
            dropped += 1
            continue
        augmented = dict(candidate)
        augmented_metrics = dict(candidate.get("metrics") or {})
        augmented_metrics.update(result.to_metrics_dict())
        augmented["metrics"] = augmented_metrics
        survivors.append(augmented)
        if len(survivors) >= candidates_per_strategy:
            break
    return survivors, dropped


if __name__ == "__main__":
    main()
