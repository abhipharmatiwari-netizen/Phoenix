"""
Advanced parameter optimization strategies for EMA20 strategy.
Demonstrates multi-objective optimization, constraint handling, and market-regime-specific tuning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
import numpy as np

from app.strategies.ml_param_optimizer import (
    ParameterSet,
    BacktestMetrics,
    Ema20Backtester,
)

logger = logging.getLogger(__name__)


@dataclass
class OptimizationConstraint:
    """Constraint on parameter values or metrics."""
    name: str
    check: Callable[[Dict[str, Any], BacktestMetrics], bool]
    reason: str


class MultiObjectiveOptimizer:
    """
    Multi-objective optimization using Pareto dominance.
    Balances multiple objectives: profitability, stability, win rate, drawdown.
    """

    def __init__(self, param_sets: List[ParameterSet]):
        self.param_sets = param_sets

    def score_weighted(
        self,
        profit_weight: float = 0.4,
        stability_weight: float = 0.3,
        win_rate_weight: float = 0.2,
        drawdown_weight: float = 0.1,
    ) -> List[Tuple[ParameterSet, float]]:
        """
        Score parameters with weighted multi-objective function.

        Args:
            profit_weight: Weight for total PnL (higher = more profit focus)
            stability_weight: Weight for Sharpe ratio (higher = less volatility)
            win_rate_weight: Weight for win rate
            drawdown_weight: Weight for max drawdown penalty

        Returns:
            List of (ParameterSet, score) tuples sorted by score descending
        """
        scored = []
        for param_set in self.param_sets:
            m = param_set.metrics

            # Normalize metrics to [0, 1]
            all_profits = [p.metrics.total_pnl for p in self.param_sets if p.metrics]
            all_drawdowns = [abs(p.metrics.max_drawdown) for p in self.param_sets if p.metrics]

            profit_score = (m.total_pnl - min(all_profits)) / (max(all_profits) - min(all_profits) + 1e-6) if all_profits else 0
            sharpe_score = (m.sharpe_ratio + 10) / 20  # Sharpe typically -10 to 10
            win_rate_score = m.win_rate
            drawdown_score = 1 - (abs(m.max_drawdown) - min(all_drawdowns)) / (max(all_drawdowns) - min(all_drawdowns) + 1e-6) if all_drawdowns else 1

            weighted_score = (
                profit_weight * profit_score +
                stability_weight * sharpe_score +
                win_rate_weight * win_rate_score +
                drawdown_weight * drawdown_score
            )

            scored.append((param_set, weighted_score))

        return sorted(scored, key=lambda x: x[1], reverse=True)

    def pareto_dominance(self) -> List[ParameterSet]:
        """
        Find Pareto-optimal solutions.
        A configuration dominates another if it's better on ALL objectives.
        """
        frontier = []

        for candidate in self.param_sets:
            dominates = False

            for other in self.param_sets:
                if candidate == other:
                    continue

                # Candidate dominates if:
                # - Higher profit AND
                # - Better win rate AND
                # - Lower drawdown
                candidate_better = (
                    candidate.metrics.total_pnl >= other.metrics.total_pnl and
                    candidate.metrics.win_rate >= other.metrics.win_rate and
                    candidate.metrics.max_drawdown >= other.metrics.max_drawdown
                )

                if candidate_better:
                    # Check if strictly better in at least one dimension
                    strictly_better = (
                        candidate.metrics.total_pnl > other.metrics.total_pnl or
                        candidate.metrics.win_rate > other.metrics.win_rate or
                        candidate.metrics.max_drawdown > other.metrics.max_drawdown
                    )
                    if strictly_better:
                        dominates = True
                        break

            if not dominates:
                frontier.append(candidate)

        return frontier

    def constraint_filter(
        self,
        constraints: List[OptimizationConstraint],
    ) -> List[ParameterSet]:
        """Filter parameter sets by constraints."""
        valid = []
        for param_set in self.param_sets:
            valid_param = True
            for constraint in constraints:
                if not constraint.check(param_set.params, param_set.metrics):
                    logger.debug(f"Parameter set rejected: {constraint.reason}")
                    valid_param = False
                    break
            if valid_param:
                valid.append(param_set)
        return valid


class MarketRegimeOptimizer:
    """
    Market regime-specific parameter optimization.
    Different regimes (trending, choppy, breakout) require different parameters.
    """

    def __init__(self, backtester: Ema20Backtester):
        self.backtester = backtester

    def detect_regime(self) -> str:
        """
        Detect current market regime from OHLC data.
        Returns: "TRENDING", "CHOPPY", "BREAKOUT", "SIDEWAYS"
        """
        df = self.backtester.ohlc_data.copy()

        # Simple heuristics
        df['range'] = (df['high'] - df['low']) / df['close']
        avg_range = df['range'].rolling(20).mean()

        df['atr_sma'] = df['high'].rolling(20).mean() - df['low'].rolling(20).mean()
        volatility = df['atr_sma'].iloc[-1] / df['close'].iloc[-1]

        # Check trend
        ema_20 = df['close'].ewm(span=20).mean()
        trend_strength = abs((df['close'].iloc[-1] - ema_20.iloc[-1]) / ema_20.iloc[-1])

        if volatility > avg_range.iloc[-1] * 1.5:
            return "BREAKOUT"
        elif trend_strength > 0.02:
            return "TRENDING"
        elif volatility < avg_range.iloc[-1] * 0.5:
            return "SIDEWAYS"
        else:
            return "CHOPPY"

    def optimal_params_for_regime(self, regime: str) -> Dict[str, Any]:
        """Return recommended parameters for market regime."""
        presets = {
            "TRENDING": {
                "ema_period": 14,  # Shorter to catch trend early
                "sl_pct": 0.15,  # Tight stop in trending market
                "tp_pct": 0.50,  # Let winners run
                "min_atr": 0.10,
                "use_adx_filter": True,  # Use trend confirmation
                "trail_buffer_pct": 0.05,  # Trail gains
            },
            "CHOPPY": {
                "ema_period": 30,  # Longer to filter noise
                "sl_pct": 0.20,  # Wider stops
                "tp_pct": 0.20,  # Quick exits
                "min_atr": 0.05,  # Lower requirement
                "use_adx_filter": False,  # Fewer false signals
                "trail_buffer_pct": 0.0,
            },
            "BREAKOUT": {
                "ema_period": 10,  # Very short for fast reaction
                "sl_pct": 0.25,
                "tp_pct": 0.60,  # Catch big moves
                "min_atr": 0.30,  # High volatility requirement
                "use_adx_filter": True,
                "trail_buffer_pct": 0.10,
            },
            "SIDEWAYS": {
                "ema_period": 20,  # Medium
                "sl_pct": 0.30,  # Standard stop
                "tp_pct": 0.25,  # Quick profit taking
                "min_atr": 0.08,
                "use_adx_filter": False,
                "trail_buffer_pct": 0.0,
            },
        }
        return presets.get(regime, presets["CHOPPY"])


class RiskAdjustedOptimizer:
    """Optimize for risk-adjusted returns (Sharpe ratio, Calmar ratio)."""

    @staticmethod
    def filter_by_sharpe(
        param_sets: List[ParameterSet],
        min_sharpe: float = 0.5,
    ) -> List[ParameterSet]:
        """Filter configurations with minimum Sharpe ratio."""
        return [p for p in param_sets if p.metrics.sharpe_ratio >= min_sharpe]

    @staticmethod
    def filter_by_max_drawdown(
        param_sets: List[ParameterSet],
        max_drawdown: float = -500.0,  # Max loss in dollars
    ) -> List[ParameterSet]:
        """Filter configurations with acceptable drawdown."""
        return [p for p in param_sets if p.metrics.max_drawdown >= max_drawdown]

    @staticmethod
    def filter_by_win_rate(
        param_sets: List[ParameterSet],
        min_win_rate: float = 0.40,
    ) -> List[ParameterSet]:
        """Filter configurations with minimum win rate."""
        return [p for p in param_sets if p.metrics.win_rate >= min_win_rate]

    @staticmethod
    def filter_by_profit_factor(
        param_sets: List[ParameterSet],
        min_profit_factor: float = 1.5,
    ) -> List[ParameterSet]:
        """Filter configurations with minimum profit factor."""
        return [p for p in param_sets if p.metrics.profit_factor >= min_profit_factor]


# Pre-defined optimization strategies

CONSERVATIVE_STRATEGY = {
    "description": "Low drawdown, stable returns (ideal for small accounts)",
    "constraints": [
        OptimizationConstraint(
            name="max_drawdown",
            check=lambda p, m: m.max_drawdown >= -200,
            reason="Max drawdown > $200"
        ),
        OptimizationConstraint(
            name="min_win_rate",
            check=lambda p, m: m.win_rate >= 0.45,
            reason="Win rate < 45%"
        ),
        OptimizationConstraint(
            name="min_trades",
            check=lambda p, m: m.total_trades >= 10,
            reason="Insufficient trade count"
        ),
    ],
    "objectives": {
        "profit_weight": 0.2,
        "stability_weight": 0.5,
        "win_rate_weight": 0.2,
        "drawdown_weight": 0.1,
    }
}

GROWTH_STRATEGY = {
    "description": "High profit focus with acceptable drawdown",
    "constraints": [
        OptimizationConstraint(
            name="max_drawdown",
            check=lambda p, m: m.max_drawdown >= -500,
            reason="Max drawdown > $500"
        ),
        OptimizationConstraint(
            name="min_trades",
            check=lambda p, m: m.total_trades >= 5,
            reason="Insufficient trade count"
        ),
    ],
    "objectives": {
        "profit_weight": 0.6,
        "stability_weight": 0.2,
        "win_rate_weight": 0.1,
        "drawdown_weight": 0.1,
    }
}

AGGRESSIVE_STRATEGY = {
    "description": "Maximum profit regardless of volatility",
    "constraints": [
        OptimizationConstraint(
            name="min_trades",
            check=lambda p, m: m.total_trades >= 3,
            reason="Insufficient trade count"
        ),
    ],
    "objectives": {
        "profit_weight": 0.8,
        "stability_weight": 0.1,
        "win_rate_weight": 0.05,
        "drawdown_weight": 0.05,
    }
}
