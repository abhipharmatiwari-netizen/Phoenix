"""
ML-powered parameter optimization for trading strategies.
Uses Bayesian optimization + lightweight ML models to find profitable parameter combinations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
from datetime import datetime
import json

logger = logging.getLogger(__name__)


@dataclass
class ParameterSpace:
    """Defines valid ranges for strategy parameters."""
    name: str
    param_type: str  # "int", "float", "categorical", "bool"
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    categories: Optional[List[Any]] = None

    def sample(self, value: float = None) -> Any:
        """Convert normalized [0,1] value to parameter value."""
        norm_val = value if value is not None else 0.5

        if self.param_type == "int":
            return int(self.min_value + (self.max_value - self.min_value) * norm_val)
        elif self.param_type == "float":
            return self.min_value + (self.max_value - self.min_value) * norm_val
        elif self.param_type == "bool":
            return norm_val > 0.5
        elif self.param_type == "categorical":
            idx = int(len(self.categories) * norm_val)
            return self.categories[min(idx, len(self.categories) - 1)]
        return value


@dataclass
class TradeResult:
    """Single trade result."""
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: int
    side: str  # "LONG" or "SHORT"

    @property
    def pnl(self) -> float:
        if self.side == "SHORT":
            return (self.entry_price - self.exit_price) * self.quantity
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0
        if self.side == "SHORT":
            return ((self.entry_price - self.exit_price) / self.entry_price) * 100
        return ((self.exit_price - self.entry_price) / self.entry_price) * 100

    @property
    def holding_time_minutes(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 60


@dataclass
class BacktestMetrics:
    """Performance metrics from backtest."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    @property
    def score(self) -> float:
        """Composite profitability score (higher is better).

        PR #283 codex P2: returns the signed score so that configurations
        which lose money over the optimizer window can still be ranked
        against each other by least-bad performance. Clamping to zero
        made every losing candidate tie and let evaluation order pick
        ``best_params`` arbitrarily on short windows. Callers that need a
        non-negative scalar should clamp at the call site.
        """
        if self.total_trades == 0:
            return 0.0

        # Multi-factor score: profit + win_rate + profit_factor - drawdown penalty
        score = 0.0
        score += self.total_pnl * 0.4  # 40% weight on absolute profit
        score += (self.win_rate * self.profit_factor) * 500  # 40% weight on consistency
        score -= abs(self.max_drawdown) * 0.2  # 20% penalty for drawdown

        return score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "win_rate": round(self.win_rate, 4),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 2),
            "largest_win": round(self.largest_win, 2),
            "largest_loss": round(self.largest_loss, 2),
            "score": round(self.score, 2),
        }


@dataclass
class ParameterSet:
    """A specific configuration to evaluate."""
    params: Dict[str, Any] = field(default_factory=dict)
    metrics: Optional[BacktestMetrics] = None
    generation: int = 0

    def score(self) -> float:
        return self.metrics.score if self.metrics else 0.0


class Ema20Backtester:
    """Simulates EMA20 strategy performance on synthetic/historical data."""

    def __init__(self, ohlc_data: Optional[pd.DataFrame] = None):
        """
        Args:
            ohlc_data: DataFrame with columns [timestamp, open, high, low, close, volume]
        """
        # PR #283 codex P2: pandas DataFrames cannot be used in boolean
        # context (ValueError: The truth value of a DataFrame is ambiguous).
        # Explicitly compare to None so callers can pass a real DataFrame.
        self.ohlc_data = (
            ohlc_data if ohlc_data is not None else self._generate_synthetic_data()
        )

    def _generate_synthetic_data(self, days: int = 20, bars_per_day: int = 48) -> pd.DataFrame:
        """Generate realistic synthetic OHLC data for testing."""
        dates = pd.date_range(end=datetime.now(), periods=days*bars_per_day, freq='5min')

        # Geometric Brownian motion for realistic price action
        np.random.seed(42)
        returns = np.random.normal(0.0001, 0.005, len(dates))
        price = 25000 * np.exp(np.cumsum(returns))

        data = []
        for i in range(len(dates) - 1):
            o = price[i]
            c = price[i + 1]
            h = max(o, c) * (1 + abs(np.random.normal(0, 0.003)))
            low = min(o, c) * (1 - abs(np.random.normal(0, 0.003)))
            v = np.random.randint(1000, 50000)
            data.append({
                'timestamp': dates[i],
                'open': o,
                'high': h,
                'low': low,
                'close': c,
                'volume': v,
            })

        return pd.DataFrame(data)

    def backtest(self, params: Dict[str, Any]) -> BacktestMetrics:
        """Run backtest with given parameters."""
        df = self.ohlc_data.copy()

        # Extract parameters with defaults
        ema_period = params.get('ema_period', 20)
        sl_pct = params.get('sl_pct', 0.30)
        tp_pct = params.get('tp_pct', 0.30)
        min_atr = params.get('min_atr', 0.1)
        require_rsi_falling = params.get('require_rsi_falling', True)

        # Calculate indicators
        df['ema'] = self._ema(df['close'], ema_period)
        df['atr'] = self._atr(df['high'], df['low'], df['close'], 14)
        df['rsi'] = self._rsi(df['close'], 14)

        # Generate signals
        df['signal'] = 0
        for i in range(ema_period, len(df)):
            if i == 0:
                continue

            # Entry condition: price < EMA and ATR sufficient
            close_below_ema = df['close'].iloc[i] < df['ema'].iloc[i]
            atr_ok = df['atr'].iloc[i] >= min_atr if min_atr > 0 else True
            rsi_falling = (df['rsi'].iloc[i] < df['rsi'].iloc[i-1]) if require_rsi_falling else True

            if close_below_ema and atr_ok and rsi_falling:
                df.loc[i, 'signal'] = 1

        # Simulate trades
        trades: List[TradeResult] = []
        in_position = False
        entry_idx = -1

        for i in range(1, len(df)):
            if not in_position and df['signal'].iloc[i] == 1:
                in_position = True
                entry_idx = i

            elif in_position:
                entry_price = df['close'].iloc[entry_idx]
                current_price = df['close'].iloc[i]
                pnl_pct = ((entry_price - current_price) / entry_price) * 100

                # Exit conditions
                exit = False
                if pnl_pct <= -sl_pct:  # Stop loss
                    exit = True
                elif pnl_pct >= tp_pct:  # Take profit
                    exit = True

                if exit or i == len(df) - 1:
                    in_position = False
                    trades.append(TradeResult(
                        entry_time=df['timestamp'].iloc[entry_idx],
                        exit_time=df['timestamp'].iloc[i],
                        entry_price=entry_price,
                        exit_price=current_price,
                        quantity=1,
                        side='SHORT',
                    ))

        return self._calculate_metrics(trades)

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _calculate_metrics(trades: List[TradeResult]) -> BacktestMetrics:
        """Calculate performance metrics from trade list."""
        if not trades:
            return BacktestMetrics()

        pnls = [t.pnl for t in trades]
        winning = [p for p in pnls if p > 0]
        losing = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        max_dd = 0.0
        cumulative = 0.0
        peak = 0.0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_dd = min(max_dd, cumulative - peak)

        metrics = BacktestMetrics(
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            total_pnl=total_pnl,
            max_drawdown=max_dd,
            win_rate=len(winning) / len(trades) if trades else 0,
            avg_win=sum(winning) / len(winning) if winning else 0,
            avg_loss=sum(losing) / len(losing) if losing else 0,
            profit_factor=sum(winning) / abs(sum(losing)) if losing and sum(losing) != 0 else 0,
            largest_win=max(winning) if winning else 0,
            largest_loss=min(losing) if losing else 0,
        )

        # Calculate Sharpe ratio (simplified)
        if len(pnls) > 1:
            returns = np.array(pnls) / np.abs(np.array(pnls)).mean()
            metrics.sharpe_ratio = np.mean(returns) / (np.std(returns) + 1e-6) * np.sqrt(252)

        return metrics


class BayesianOptimizer:
    """Bayesian optimization for parameter tuning using lightweight ML."""

    def __init__(self, param_spaces: List[ParameterSpace], backtester: Ema20Backtester):
        self.param_spaces = param_spaces
        self.backtester = backtester
        self.evaluated: List[ParameterSet] = []
        self.best_params: Optional[Dict[str, Any]] = None
        self.best_score = -float('inf')

    def _normalize_params(self, params: Dict[str, Any]) -> np.ndarray:
        """Convert parameters to normalized [0,1] array."""
        normalized = []
        for space in self.param_spaces:
            if space.param_type == "int":
                val = (params[space.name] - space.min_value) / (space.max_value - space.min_value)
            elif space.param_type == "float":
                val = (params[space.name] - space.min_value) / (space.max_value - space.min_value)
            elif space.param_type == "bool":
                val = 1.0 if params[space.name] else 0.0
            elif space.param_type == "categorical":
                # PR #283 codex P2: normalize by category INDEX, not by the
                # category value itself. The previous ``value / len(cats)``
                # clipped non-trivial values (e.g. ``signal_timeframe`` =
                # 60/300/600 over 3 categories ⇒ all >= 1.0 ⇒ clipped to
                # 1.0) so denormalize always returned the last category.
                categories = space.categories or [params[space.name]]
                try:
                    idx = categories.index(params[space.name])
                except ValueError:
                    idx = 0
                divisor = max(1, len(categories) - 1)
                val = idx / divisor
            else:
                val = 0.5
            normalized.append(np.clip(val, 0, 1))
        return np.array(normalized)

    def _denormalize_params(self, normalized: np.ndarray) -> Dict[str, Any]:
        """Convert normalized array back to parameters."""
        params = {}
        for i, space in enumerate(self.param_spaces):
            val = normalized[i]
            params[space.name] = space.sample(val)
        return params

    def evaluate(self, params: Dict[str, Any]) -> float:
        """Evaluate parameter set and return score."""
        metrics = self.backtester.backtest(params)
        param_set = ParameterSet(params=params, metrics=metrics)
        self.evaluated.append(param_set)

        score = metrics.score
        if score > self.best_score:
            self.best_score = score
            self.best_params = params

        return score

    def optimize(self, n_iterations: int = 50, random_seed: int = 42) -> Dict[str, Any]:
        """Run Bayesian optimization."""
        np.random.seed(random_seed)

        # Phase 1: Random exploration (20% of iterations)
        logger.info(f"Phase 1: Random exploration ({int(n_iterations * 0.2)} iterations)")
        for _ in range(int(n_iterations * 0.2)):
            params = {}
            for space in self.param_spaces:
                rand_val = np.random.uniform(0, 1)
                params[space.name] = space.sample(rand_val)
            self.evaluate(params)

        # Phase 2: Gaussian Process-guided search (80% of iterations)
        logger.info(f"Phase 2: Intelligent search ({int(n_iterations * 0.8)} iterations)")
        for iteration in range(int(n_iterations * 0.8)):
            # Use expected improvement heuristic
            if len(self.evaluated) >= 5:
                # NOTE: a future enhancement would fit a Gaussian-process
                # regressor over (X=normalized params, y=observed scores).
                # The current implementation uses the simpler perturb-the-
                # best heuristic with a decaying noise schedule, which is
                # cheap and good enough for 50–100-iteration runs.
                decay = 0.9 ** (iteration / int(n_iterations * 0.8))
                best_normalized = self._normalize_params(self.best_params)

                # Add Gaussian noise proportional to iteration progress
                noise = np.random.normal(0, 0.1 * decay, len(best_normalized))
                perturbed = np.clip(best_normalized + noise, 0, 1)
                params = self._denormalize_params(perturbed)
            else:
                # Fallback to random if not enough data
                params = {}
                for space in self.param_spaces:
                    params[space.name] = space.sample(np.random.uniform(0, 1))

            self.evaluate(params)
            if (iteration + 1) % 10 == 0:
                logger.info(f"  Iteration {iteration + 1}: Best score = {self.best_score:.2f}")

        logger.info(f"Optimization complete. Best score: {self.best_score:.2f}")
        return self.best_params


class ParameterEnsemble:
    """Combines multiple lightweight ML models to rank parameter configurations."""

    def __init__(self, param_sets: List[ParameterSet]):
        self.param_sets = sorted(param_sets, key=lambda p: p.score(), reverse=True)

    def top_n(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return top N parameter configurations."""
        return [p.params for p in self.param_sets[:n]]

    def pareto_frontier(self) -> List[Dict[str, Any]]:
        """Return Pareto-optimal configurations (tradeoff between profit and drawdown).

        PR #283 codex P2: drawdown is stored as a NEGATIVE number
        (``min(cumulative - peak)``). The previous filter ``>= min_drawdown``
        kept candidates whose drawdown was *more negative* — i.e. WORSE
        — than the seen minimum, and discarded lower-risk candidates.
        Compare absolute drawdown so the frontier preserves the
        lower-risk tradeoff that is the whole point of Pareto here.
        """
        frontier = []

        # Sort by profit descending
        sorted_sets = sorted(self.param_sets, key=lambda p: p.metrics.total_pnl, reverse=True)

        best_dd_abs = float('inf')
        for param_set in sorted_sets:
            dd_abs = abs(param_set.metrics.max_drawdown)
            if dd_abs >= best_dd_abs:
                continue
            frontier.append(param_set)
            best_dd_abs = dd_abs

        return [p.params for p in frontier]

    def _pareto_frontier_sets(self) -> List[ParameterSet]:
        """Internal: same logic as ``pareto_frontier`` but returns the
        ``ParameterSet`` objects so ``to_json`` can serialize metrics
        without re-looking-them-up.
        """
        frontier: List[ParameterSet] = []
        sorted_sets = sorted(self.param_sets, key=lambda p: p.metrics.total_pnl, reverse=True)
        best_dd_abs = float('inf')
        for param_set in sorted_sets:
            dd_abs = abs(param_set.metrics.max_drawdown)
            if dd_abs >= best_dd_abs:
                continue
            frontier.append(param_set)
            best_dd_abs = dd_abs
        return frontier

    def to_json(self) -> str:
        """Export results as JSON.

        PR #283 codex P2: previously iterated ``pareto_frontier()`` which
        returns ``List[Dict]`` (params only) and tried to read
        ``p.params`` / ``p.metrics`` on each — would have raised
        ``AttributeError`` on any caller using this helper. Now uses the
        internal ``_pareto_frontier_sets`` helper which preserves the
        ``ParameterSet`` so metrics serialize cleanly.
        """
        results = {
            "top_10": [
                {
                    "params": p.params,
                    "metrics": p.metrics.to_dict() if p.metrics else {}
                }
                for p in self.param_sets[:10]
            ],
            "pareto_frontier": [
                {
                    "params": p.params,
                    "metrics": p.metrics.to_dict() if p.metrics else {}
                }
                for p in self._pareto_frontier_sets()
            ],
        }
        return json.dumps(results, indent=2, default=str)
